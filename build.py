#!/usr/bin/env python3
"""Build the Ingest Station USB installer (dist/usb/) on Windows with only
Python: downloads and verifies the official Alpine artifacts, resolves the
packages from the signed APKINDEX, builds the apkovl and the extra initrd.

Usage:
    python build.py                 build dist/usb/
    python build.py --offline       use only the download cache
    python build.py --deploy HOST   build, then update the station at HOST over SSH
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from buildlib import apk, assets, cpio, initrd, sshkey  # noqa: E402
from buildlib.iso9660 import ISO9660  # noqa: E402
from buildlib.overlay import Overlay  # noqa: E402
from buildlib.squashfs import SquashFS  # noqa: E402
from ingest import __version__  # noqa: E402

CACHE = os.path.join(ROOT, "cache")
SSH_KEY = os.path.join(ROOT, "keys", "private", "ssh_ed25519")
DIST = os.path.join(ROOT, "dist")
BOOT = os.path.join(ROOT, "buildlib", "boot")

RUNLEVELS = {
    "sysinit": ["devfs", "dmesg", "mdev", "hwdrivers", "modloop"],
    "boot": ["modules", "sysctl", "hostname", "bootmisc", "syslog", "hwclock",
             "ingest-watchdog", "ingest-data"],
    "default": ["ingest-network", "ingest-console", "avahi-daemon", "ntpd", "ingest-sshkeys",
                "sshd", "ingestd", "ingest-gui", "ingest-bootok"],
    "shutdown": ["mount-ro", "killprocs", "savecache"],
}
TIMEZONE = "Europe/Rome"


class BuildError(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# downloads

class Fetcher:
    def __init__(self, offline: bool):
        self.offline = offline

    def get(self, url: str, dest: str, refresh: bool = False) -> str:
        if os.path.exists(dest) and not refresh:
            return dest
        if self.offline:
            if os.path.exists(dest):
                return dest
            raise BuildError(f"--offline: {os.path.relpath(dest, ROOT)} not in cache")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        say(f"\r  download {url}")
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "ingest-station-build"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(tmp, dest)
        return dest


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def find_gpg() -> str | None:
    for cand in (shutil.which("gpg"), r"C:\Program Files\Git\usr\bin\gpg.exe"):
        if cand and os.path.exists(cand):
            return cand
    return None


def msys_path(path: str) -> str:
    """C:\\x\\y -> /c/x/y, the form expected by Git for Windows' gpg."""
    path = os.path.abspath(path)
    drive, rest = os.path.splitdrive(path)
    if not drive:
        return path.replace("\\", "/")
    return f"/{drive[0].lower()}{rest.replace(os.sep, '/')}"


def gpg_verify(sig: str, data: str, key: str, fingerprint: str) -> bool:
    """Verify a detached signature with a pinned key. Returns False if gpg
    is not available."""
    gpg = find_gpg()
    if not gpg:
        return False
    home = tempfile.mkdtemp(prefix="ingest-gpg-")
    try:
        fwd = msys_path if "usr" in os.path.normcase(gpg).split(os.sep) else (lambda p: p)
        base = [gpg, "--batch", "--no-tty", "--homedir", fwd(home)]
        subprocess.run(base + ["--import", fwd(key)], check=True, capture_output=True)
        res = subprocess.run(base + ["--status-fd", "1", "--verify", fwd(sig), fwd(data)],
                             capture_output=True, text=True)
        valid = [l.split()[2] for l in res.stdout.splitlines() if l.startswith("[GNUPG:] VALIDSIG")]
        if fingerprint not in valid:
            raise BuildError(f"GPG signature check failed for {os.path.basename(data)}")
        return True
    finally:
        shutil.rmtree(home, ignore_errors=True)


# --------------------------------------------------------------------------
# build steps

def fetch_iso(cfg: dict, fx: Fetcher, require_gpg: bool) -> str:
    a = cfg["alpine"]
    name = f"alpine-{a['iso_flavor']}-{a['version']}-{a['arch']}.iso"
    base = f"{a['mirror']}/{a['branch']}/releases/{a['arch']}"
    iso = fx.get(f"{base}/{name}", os.path.join(CACHE, name))
    sha = fx.get(f"{base}/{name}.sha256", os.path.join(CACHE, name + ".sha256"))
    asc = fx.get(f"{base}/{name}.asc", os.path.join(CACHE, name + ".asc"))
    want = open(sha, encoding="ascii").read().split()[0]
    if sha256_file(iso) != want:
        os.remove(iso)
        raise BuildError(f"{name}: SHA256 mismatch (deleted, run again)")
    say(f"  {name}: SHA256 ok")
    if gpg_verify(asc, iso, os.path.join(ROOT, "keys", "alpine-ncopa.asc"), a["gpg_fingerprint"]):
        say(f"  {name}: GPG signature ok ({a['gpg_fingerprint'][-16:]})")
    elif require_gpg:
        raise BuildError("gpg not found: install Git for Windows or pass --no-gpg")
    else:
        say("  WARNING: gpg not found, ISO signature NOT verified (SHA256 only)")
    return iso


def load_indexes(cfg: dict, fx: Fetcher, keys: dict[str, bytes]) -> dict[str, list[apk.Package]]:
    a = cfg["alpine"]
    out = {}
    for repo in a["repos"]:
        url = f"{a['mirror']}/{a['branch']}/{repo}/{a['arch']}/APKINDEX.tar.gz"
        path = fx.get(url, os.path.join(CACHE, "apks", repo, "APKINDEX.tar.gz"),
                      refresh=not fx.offline)
        out[repo] = apk.load_apkindex(open(path, "rb").read(), repo, keys)
        say(f"  APKINDEX {repo}: signature ok, {len(out[repo])} packages")
    return out


def fetch_package(cfg: dict, fx: Fetcher, pkg: apk.Package) -> bytes:
    a = cfg["alpine"]
    path = os.path.join(CACHE, "apks", pkg.repo, pkg.filename)
    url = f"{a['mirror']}/{a['branch']}/{pkg.repo}/{a['arch']}/{pkg.filename}"
    for attempt in (1, 2):
        fx.get(url, path)
        raw = open(path, "rb").read()
        try:
            apk.verify_apk(raw, pkg)
            return raw
        except apk.VerifyError:
            os.remove(path)
            if attempt == 2 or fx.offline:
                raise
    raise AssertionError


def grub_skip_iso_search(efi: bytes) -> bytes:
    """The ISO's GRUB image embeds an early config that looks for the ISO by
    its label: on the station it scans every disk for a few seconds and
    prints "error: no such device", then goes on with the device it was
    loaded from anyway. Replace that line with a harmless command of the
    same length (the embedded config keeps its size). Not a comment: this
    config runs in GRUB's minimal parser, which has no comments
    ("invalid command #")."""
    found = re.findall(rb'search --no-floppy --set=root --label "[^"\n]*"\n', efi)
    if len(found) != 1:
        raise BuildError("BOOTX64.EFI: the embedded GRUB config was not found")
    line = found[0]
    noop = b"set ingest_root=boot_device"
    return efi.replace(line, noop.ljust(len(line) - 1, b"_") + b"\n")


def grub_modules(cfg: dict, fx: Fetcher, packages: dict, efi: bytes) -> dict[str, bytes]:
    """Runtime GRUB modules from the grub-efi package, after checking that
    the package is the same build as the ISO's BOOTX64.EFI."""
    by_name = {p.name: p for ps in packages.values() for p in ps}
    raw = fetch_package(cfg, fx, by_name["grub-efi"])
    tar = apk.apk_data_members(raw)
    mods = {}
    moddep = ""
    for m in tar.getmembers():
        if m.isfile() and "/x86_64-efi/" in m.name:
            data = tar.extractfile(m).read()
            base = m.name.rsplit("/", 1)[1]
            if base.endswith(".mod"):
                mods[base[:-4]] = data
            elif base == "moddep.lst":
                moddep = data.decode()
    for probe in ("normal", "fat", "linux", "search"):
        if mods[probe] not in efi:
            raise BuildError(f"grub-efi {by_name['grub-efi'].version} does not match the ISO's "
                             f"BOOTX64.EFI (module {probe} differs)")
    deps = {l.split(":")[0]: l.split(":")[1].split() for l in moddep.splitlines() if ":" in l}
    wanted, stack = set(), list(cfg["grub"]["runtime_modules"])
    while stack:
        m = stack.pop()
        if m not in wanted:
            wanted.add(m)
            stack.extend(deps.get(m, []))
    out = {f"{m}.mod": mods[m] for m in sorted(wanted)}
    out["moddep.lst"] = moddep.encode()
    return out


def kernel_cmdline(cfg: dict, watchdog: int) -> str:
    k = cfg["kernel"]
    # the iTCO module can be auto-loaded before early.sh runs, so its
    # heartbeat is set as a module parameter on the command line
    wdt = ([f"wdat_wdt.timeout={watchdog}", f"iTCO_wdt.heartbeat={watchdog}",
            f"ingest_wdt={watchdog}"] if watchdog else [])
    return " ".join([f"modules={','.join(k['initramfs_modules'])}", *k["cmdline"], *wdt])


def vendor_packages(cfg: dict, fx: Fetcher, resolver: apk.Resolver,
                    selected: dict) -> dict[str, tuple[bytes, int]]:
    """Files of the vendored packages (site-packages only), after checking
    that each of their dependencies is satisfied by the installed set."""
    v = cfg.get("vendor", {})
    skip = set(v.get("skip_deps", []))
    out: dict[str, tuple[bytes, int]] = {}
    for name in v.get("apk", []):
        pkg = resolver.by_name[name]
        for dep in pkg.depends:
            neg, dname = apk._dep_name(dep)
            if neg or dname in skip or dname.startswith("python3"):
                continue
            try:
                provider = resolver._pick(dname, selected)
            except KeyError:
                raise BuildError(f"vendored {name}: nothing provides {dname}")
            if provider.name in skip:
                continue
            if provider.name not in selected and not dname.startswith("py3-pygame"):
                raise BuildError(f"vendored {name} needs {dname}: add {provider.name} "
                                 "to [packages] system")
        tar = apk.apk_data_members(fetch_package(cfg, fx, pkg))
        for m in tar.getmembers():
            marker = "/site-packages/"
            if m.isfile() and marker in m.name:
                rel = m.name.split(marker, 1)[1]
                out[f"opt/ingest/vendor/{rel}"] = (tar.extractfile(m).read(), m.mode & 0o777)
    return out


def gui_assets(cfg: dict, fx: Fetcher, resolver: apk.Resolver) -> dict[str, tuple[bytes, int]]:
    """Inter font faces and vendored wheels for the GUI."""
    a = cfg.get("assets", {})
    out: dict[str, tuple[bytes, int]] = {}
    if a.get("font_package"):
        raw = fetch_package(cfg, fx, resolver.by_name[a["font_package"]])
        tar = apk.apk_data_members(raw)
        ttc = next((tar.extractfile(m).read() for m in tar.getmembers()
                    if m.isfile() and m.name.endswith(".ttc")), None)
        if ttc is None:
            raise BuildError(f"{a['font_package']}: no font collection found")
        for name, data in assets.inter_fonts(ttc).items():
            out[f"opt/ingest/assets/fonts/{name}"] = (data, 0o644)
        out["opt/ingest/assets/fonts/LICENSE-Inter.txt"] = (assets.FONT_NOTE, 0o644)
    for w in a.get("wheel", []):
        path = fx.get(w["url"], os.path.join(CACHE, "wheels", w["url"].rsplit("/", 1)[1]))
        for name, data in assets.wheel_files(open(path, "rb").read(), w["sha256"],
                                             w["package"]).items():
            out[f"opt/ingest/vendor/{name}"] = (data, 0o644)
    return out


def build_overlay(world: list[str], mtime: int, vendored: dict | None = None) -> bytes:
    ov = Overlay(mtime=mtime)
    ov.add_tree(os.path.join(ROOT, "overlay"))
    ov.dir("root", 0o700)
    ov.dir("root/.ssh", 0o700)
    ov.file("root/.ssh/authorized_keys",
            sshkey.authorized_keys(sshkey.ensure_key(SSH_KEY),
                                   os.path.join(ROOT, "keys", "authorized_keys")), 0o600)
    ov.add_tree(os.path.join(ROOT, "src", "ingest"), "opt/ingest/ingest", lambda p: 0o644)
    ov.add_tree(os.path.join(ROOT, "installer"), "opt/ingest/installer", lambda p: 0o644)
    for path, (data, mode) in (vendored or {}).items():
        ov.file(path, data, mode or 0o644)
    ov.file("opt/ingest/VERSION", f"{__version__}\n".encode())
    ov.file("etc/apk/world", ("\n".join(world) + "\n").encode())
    ov.symlink("etc/localtime", f"/usr/share/zoneinfo/{TIMEZONE}")
    ov.file("etc/timezone", f"{TIMEZONE}\n".encode())
    for level, services in RUNLEVELS.items():
        ov.dir(f"etc/runlevels/{level}")
        for s in services:
            ov.symlink(f"etc/runlevels/{level}/{s}", f"/etc/init.d/{s}")
    return ov.tar_gz()


def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def fill_template(name: str, cmdline: str, installer_extra: str = "") -> bytes:
    text = open(os.path.join(BOOT, name), encoding="utf-8").read()
    return (text.replace("@CMDLINE@", cmdline).replace("@VERSION@", __version__)
            .replace(" @INSTALLER_EXTRA@", f" {installer_extra}" if installer_extra else "")
            .encode())


def build_usb(args) -> None:
    t0 = time.time()
    cfg = tomllib.load(open(os.path.join(ROOT, "buildlib", "config.toml"), "rb"))
    fx = Fetcher(args.offline)
    mtime = int(time.time())

    say("[1/7] Alpine ISO")
    iso_path = fetch_iso(cfg, fx, require_gpg=not args.no_gpg)
    iso = ISO9660(iso_path)
    base_initramfs = iso.read("/boot/initramfs-lts")
    base_entries = list(cpio.read_entries(gzip.decompress(base_initramfs)))
    kver = initrd.initramfs_kernel_version(base_entries)
    efi = grub_skip_iso_search(iso.read("/efi/boot/bootx64.efi"))
    keys = {e.name.rsplit("/", 1)[1]: e.data for e in base_entries
            if e.name.startswith("etc/apk/keys/") and e.name.endswith(".pub")}
    modloop_off, _ = iso.offset("/boot/modloop-lts")
    modloop = SquashFS(iso.f, modloop_off)
    if kver not in modloop.listdir(modloop.lookup("/modules")):
        raise BuildError(f"modloop does not contain modules for kernel {kver}")
    say(f"  kernel {kver}, {len(keys)} Alpine signing keys")

    say("[2/7] Package indexes")
    indexes = load_indexes(cfg, fx, keys)

    say("[3/7] Resolving packages")
    resolver = apk.Resolver([p for ps in indexes.values() for p in ps])
    selected = resolver.resolve(cfg["packages"]["system"])
    world = sorted(selected)
    dl = sum(p.size for p in selected.values())
    inst = sum(p.installed_size for p in selected.values())
    say(f"  {len(selected)} packages, {dl / 2**20:.1f} MiB download, {inst / 2**20:.1f} MiB in RAM")

    out = os.path.join(DIST, "usb-vm" if args.test_vm else "usb")
    slot = os.path.join(out, "slots", "A")
    if os.path.exists(out):
        shutil.rmtree(out)

    say("[4/7] Downloading and verifying packages")
    for i, pkg in enumerate(sorted(selected.values(), key=lambda p: p.name), 1):
        raw = fetch_package(cfg, fx, pkg)
        write(os.path.join(slot, "apks", pkg.repo, cfg["alpine"]["arch"], pkg.filename), raw)
        print(f"\r  {i}/{len(selected)} verified", end="", flush=True)
    print()
    for repo in indexes:
        # the official signed index is copied untouched; apk only fetches
        # the packages it needs, which are all present next to it
        dest = os.path.join(slot, "apks", repo, cfg["alpine"]["arch"])
        os.makedirs(dest, exist_ok=True)
        shutil.copyfile(os.path.join(CACHE, "apks", repo, "APKINDEX.tar.gz"),
                        os.path.join(dest, "APKINDEX.tar.gz"))

    say("[5/7] Kernel, initramfs and bootloader")
    iso.extract("/boot/vmlinuz-lts", os.path.join(slot, "vmlinuz-lts"))
    write(os.path.join(slot, "initramfs-lts"), base_initramfs)
    iso.extract("/boot/modloop-lts", os.path.join(slot, "modloop-lts"))
    extra, added = initrd.build(
        base_entries, modloop, kver, cfg["kernel"]["extra_initrd_modules"],
        background_modules=cfg["kernel"].get("background_initrd_modules", []),
        early_sh=open(os.path.join(BOOT, "early.sh"), "rb").read(),
        modprobe_conf=open(os.path.join(ROOT, "overlay", "etc", "modprobe.d",
                                        "ingest-wyse.conf"), "rb").read())
    write(os.path.join(slot, "ingest-initrd.gz"), extra)
    say(f"  ingest-initrd.gz: {len(extra) // 1024} KiB, added modules: "
        + ", ".join(m.rsplit("/", 1)[1] for m in added))
    installer_extra = ""
    if args.test_vm:
        # QEMU test build: console on the serial port, shorter watchdog and
        # countdown, accept the emulated (removable) eMMC as install target
        cmdline = kernel_cmdline(cfg, 60).replace(" quiet loglevel=3", "") + " console=ttyS0,115200"
        installer_extra = "ingest_test_target=mmcblk0 ingest_test_countdown=2 ingest_test_serial"
    else:
        cmdline = kernel_cmdline(cfg, cfg["kernel"]["watchdog_seconds"])
    mods = grub_modules(cfg, fx, indexes, efi)
    write(os.path.join(slot, "boot", "BOOTX64.EFI"), efi)
    write(os.path.join(slot, "boot", "grub.cfg"), fill_template(
        "grub-system.cfg", " ".join([cmdline, *cfg["kernel"]["system_cmdline"]])))
    for name, data in mods.items():
        write(os.path.join(slot, "boot", "x86_64-efi", name), data)
    say(f"  GRUB runtime modules: {', '.join(sorted(n for n in mods if n.endswith('.mod')))}")

    say("[6/7] System overlay")
    vendored = vendor_packages(cfg, fx, resolver, selected)
    vendored.update(gui_assets(cfg, fx, resolver))
    ovl = build_overlay(world, mtime, vendored)
    write(os.path.join(slot, "ingest.apkovl.tar.gz"), ovl)
    say(f"  ingest.apkovl.tar.gz: {len(ovl) // 1024} KiB")

    files = {}
    for root, _dirs, names in os.walk(slot):
        for n in names:
            p = os.path.join(root, n)
            files[os.path.relpath(p, slot).replace(os.sep, "/")] = sha256_file(p)
    manifest = {
        "version": __version__,
        "alpine": cfg["alpine"]["version"],
        "kernel": kver,
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "packages": {p.name: p.version for p in selected.values()},
        "files": dict(sorted(files.items())),
    }
    write(os.path.join(slot, "slot.json"), json.dumps(manifest, indent=1).encode())

    say("[7/7] USB layout")
    write(os.path.join(out, "EFI", "BOOT", "BOOTX64.EFI"), efi)
    # empty marker so that Alpine's boot media scan stops waiting early
    write(os.path.join(out, "slots", ".boot_repository"), b"")
    write(os.path.join(out, "boot", "grub", "grub.cfg"), fill_template("grub-installer.cfg", cmdline,
                                                                     installer_extra))
    for name, data in mods.items():
        write(os.path.join(out, "boot", "grub", "x86_64-efi", name), data)
    write(os.path.join(out, "README.txt"), (
        f"Ingest Station {__version__} - USB installer\r\n\r\n"
        "Copy the whole content of this folder to the root of an empty FAT32 USB stick,\r\n"
        "boot the Wyse 3040 from it (F12) and follow the instructions on screen.\r\n").encode())

    total = sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(out) for n in ns)
    iso.close()
    rel = os.path.relpath(out, ROOT).replace(os.sep, "/")
    say(f"\nDone in {time.time() - t0:.0f} s: {rel}/ ({total / 2**20:.0f} MiB), "
        f"version {__version__}, Alpine {cfg['alpine']['version']}, kernel {kver}")
    say(f"Copy the CONTENT of {rel}/ to the root of a FAT32 USB stick.")


def main() -> int:
    p = argparse.ArgumentParser(description="Build the Ingest Station USB installer")
    p.add_argument("--offline", action="store_true", help="use only the download cache")
    p.add_argument("--test-vm", action="store_true",
                   help="build dist/usb-vm/ for QEMU tests (serial console, test-only options)")
    p.add_argument("--deploy", metavar="HOST",
                   help="after building, send the new system to the station at HOST[:PORT] "
                        "over SSH (A/B update with automatic rollback)")
    p.add_argument("--no-gpg", action="store_true",
                   help="allow building when gpg is not installed (SHA256 only)")
    args = p.parse_args()
    try:
        build_usb(args)
    except (BuildError, apk.VerifyError, initrd.InitrdError) as e:
        say(f"\nBUILD FAILED: {e}")
        return 1
    if args.deploy:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import wyse
        slot = os.path.join(DIST, "usb-vm" if args.test_vm else "usb", "slots", "A")
        say(f"\nDeploying to {args.deploy}")
        try:
            return wyse.deploy(args.deploy, slot)
        except (wyse.DeployError, OSError, wyse.paramiko.SSHException) as e:
            say(f"DEPLOY FAILED: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
