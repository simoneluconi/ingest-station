"""Build ingest-initrd.gz: a small cpio archive that GRUB loads after the
official Alpine initramfs. The kernel unpacks both into the same rootfs, so
files here add to or replace the official ones:

- /init, patched with a one-line hook that sources /ingest/early.sh
- /ingest/early.sh (slot selection + watchdog + background driver loading)
- /init also waits for the background drivers before switch_root
- extra kernel modules and the full modules.* index from the modloop
- /etc/modprobe.d/ingest-wyse.conf (dw_dmac blacklist)
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import stat

from . import cpio
from .squashfs import SquashFS

# Alpine's boot media scan, in the official /init
SCAN = (
    "$MOCK nlplug-findfs $cryptopts -p /sbin/mdev ${KOPT_debug_init:+-d} \\\n"
    "\t${KOPT_usbdelay:+-t $((KOPT_usbdelay*1000))} \\\n"
    "\t${KOPT_uevent_buf_size:+-U $KOPT_uevent_buf_size} \\\n"
    '\t$repoopts -a "$ROOT"/tmp/apkovls\n'
)
HOOK_ANCHOR = SCAN + "eend $?\n"
# nlplug-findfs probes every disk (loading the drivers of every device and
# waiting for USB: 3-5 s on the Wyse 3040) and fails when it finds no apkovl
# or .boot_repository where it looks (our slots are deeper on purpose).
# On an eMMC boot the boot media is known by UUID and early.sh mounts it, so
# the scan is skipped; it still runs for the USB installer and rescue (the
# USB host driver is loaded by the scan), and as a fallback when early.sh
# did not find the slot. The scan counts as successful when the hook
# located the slot, otherwise eend would drop to the recovery shell.
HOOK_REPLACEMENT = (
    "# ingest-station: skip the generic scan on an eMMC boot\n"
    'ingest_scan=yes\n'
    'case " $(cat /proc/cmdline) " in\n'
    '*" ingest_mode="*) ;;\n'
    '*" ingest_slot="*) ingest_scan= ;;\n'
    'esac\n'
    'ingest_rc=1\n'
    'if [ -n "$ingest_scan" ]; then\n'
    + SCAN +
    "\tingest_rc=$?\n"
    "fi\n"
    "# ingest-station: slot-aware boot media and early watchdog\n"
    "[ -r /ingest/early.sh ] && . /ingest/early.sh\n"
    'if [ -z "$ingest_slot_ok" ] && [ -z "$ingest_scan" ]; then\n'
    "\t# fallback: the full scan, then the hook again\n"
    + SCAN +
    "\t. /ingest/early.sh\n"
    "fi\n"
    '[ -n "$ingest_slot_ok" ] && ingest_rc=0\n'
    "eend $ingest_rc\n"
)


# Alpine's package install into the RAM root (bind mounts around apk add)
INSTALL_ANCHOR = "".join(line + "\n" for line in [
    'mkdir -p "$sysroot"/sys "$sysroot"/proc "$sysroot"/dev',
    '$MOCK mount -o bind /sys "$sysroot"/sys',
    '$MOCK mount -o bind /proc "$sysroot"/proc',
    '$MOCK mount -o bind /dev "$sysroot"/dev',
    'if [ -n "$ovlfiles" ]; then',
    '\t# shellcheck disable=SC2086',
    '\tif ! $MOCK apk add --root $sysroot $repo_opt $apkflags $pkgs < "$ovlfiles"; then',
    '\t\techo "WARNING: apkovl packages install completed with errors..." > "$ROOT"/dev/kmsg',
    '\tfi',
    'else',
    '\t# shellcheck disable=SC2086',
    '\tif ! $MOCK apk add --root $sysroot $repo_opt $apkflags $pkgs; then',
    '\t\techo "WARNING: apkovl packages install completed with errors..." > "$ROOT"/dev/kmsg',
    '\tfi',
    'fi',
    '$MOCK umount "$sysroot"/sys "$sysroot"/proc "$sysroot"/dev',
])
# the boot cache (early.sh): the root of this system version extracted from
# the data partition (~1.3 s) instead of installing the packages (~4 s); the
# first boot of a version installs them and saves the cache
INSTALL_REPLACEMENT = (
    "# ingest-station: boot cache\n"
    "if type ingest_cache_restore >/dev/null 2>&1 && ingest_cache_restore; then\n"
    "\t:\n"
    "else\n"
    + INSTALL_ANCHOR +
    "type ingest_prune_services >/dev/null 2>&1 && ingest_prune_services\n"
    "type ingest_cache_save >/dev/null 2>&1 && ingest_cache_save\n"
    "fi\n"
)


# the final switch_root (an indented one, for another boot mode, comes first)
SWITCH_ANCHOR = '\nexec switch_root $switch_root_opts $sysroot $chart_init "$KOPT_init" $KOPT_init_args\n'
# early.sh loads slow drivers (i915) in the background while /init installs
# the packages; they must be loaded before switch_root frees the initramfs
# holding the module files.
SWITCH_REPLACEMENT = (
    "\n# ingest-station: wait for the drivers loaded in the background\n"
    '[ -n "$ingest_bg_pid" ] && wait "$ingest_bg_pid"'
    + SWITCH_ANCHOR
)


class InitrdError(Exception):
    pass


def patch_init(init_text: str) -> str:
    for anchor in (HOOK_ANCHOR, INSTALL_ANCHOR, SWITCH_ANCHOR):
        if init_text.count(anchor) != 1:
            raise InitrdError(
                "Alpine /init changed: hook anchor not found exactly once; "
                "review buildlib/initrd.py for this Alpine release")
    init_text = init_text.replace(HOOK_ANCHOR, HOOK_REPLACEMENT, 1)
    init_text = init_text.replace(INSTALL_ANCHOR, INSTALL_REPLACEMENT, 1)
    return init_text.replace(SWITCH_ANCHOR, SWITCH_REPLACEMENT, 1)


def module_closure(modules_dep: str, wanted: list[str]) -> list[str]:
    """Return relative module paths (kernel/...) for `wanted` module names
    plus all their dependencies, from a modules.dep text."""
    deps: dict[str, list[str]] = {}
    by_name: dict[str, str] = {}
    for line in modules_dep.splitlines():
        if ":" not in line:
            continue
        mod, rest = line.split(":", 1)
        deps[mod] = rest.split()
        name = posixpath.basename(mod).split(".ko")[0]
        by_name[name.replace("-", "_")] = mod
    out: list[str] = []
    stack = []
    for w in wanted:
        key = w.replace("-", "_")
        if key not in by_name:
            raise InitrdError(f"module {w} not found in modloop")
        stack.append(by_name[key])
    while stack:
        m = stack.pop()
        if m in out:
            continue
        out.append(m)
        stack.extend(deps.get(m, []))
    return sorted(out)


def _firmware_refs(ko: bytes) -> set[str]:
    """The firmware= entries of a module's modinfo (may contain globs)."""
    return {m.decode(errors="replace")
            for m in re.findall(rb"(?:^|\0)firmware=([^\0]+)", ko)}


def slim(base_entries: list[cpio.Entry], kver: str, drop: list[str],
         keep: list[str]) -> tuple[list[cpio.Entry], int]:
    """Drop whole driver families from the official initramfs.

    It carries the boot drivers of every machine Alpine supports (74 MB
    uncompressed, 22 MB that GRUB reads through the firmware at ~10 MB/s).
    `drop` lists paths under kernel/ (folders, or single modules without
    .ko); a module is removed only if no kept module depends on it, and
    `keep` names modules to keep anyway. Firmware files stay only if a kept
    module asks for them. Return (entries, number of modules removed)."""
    moddir = f"usr/lib/modules/{kver}/"
    names = {e.name: e for e in base_entries}
    dep = names.get(moddir + "modules.dep")
    if dep is None:
        raise InitrdError(f"official initramfs has no {moddir}modules.dep")
    mods = {e.name[len(moddir):]: e for e in base_entries
            if e.name.startswith(moddir + "kernel/") and is_regular(e)}

    def dropped(rel: str) -> bool:
        r = rel[len("kernel/"):]
        return any(r == p or r.startswith(p + "/") or r.startswith(p + ".ko")
                   for p in drop)

    wanted = [posixpath.basename(r).split(".ko")[0] for r in mods if not dropped(r)]
    kept = set(module_closure(dep.data.decode(), wanted + list(keep)))
    firmware = set()
    for rel in kept:
        if rel in mods:
            firmware |= _firmware_refs(mods[rel].data)

    def needed_firmware(name: str) -> bool:
        rel = re.sub(r"\.(zst|xz|gz)$", "", name[len("usr/lib/firmware/"):])
        return any(fnmatch.fnmatchcase(rel, p) for p in firmware)

    out = [e for e in base_entries
           if not (e.name[len(moddir):] in mods and e.name[len(moddir):] not in kept)
           and not (e.name.startswith("usr/lib/firmware/") and is_regular(e)
                    and not needed_firmware(e.name))]
    return out, len(mods) - len(kept & mods.keys())


def write_entries(entries: list[cpio.Entry]) -> bytes:
    """Gzip'd cpio of regular files, folders and symlinks, in order."""
    w = cpio.Writer()
    for e in entries:
        if stat.S_ISDIR(e.mode):
            w.directory(e.name, e.mode & 0o7777)
        elif stat.S_ISLNK(e.mode):
            w.symlink(e.name, e.data.decode())
        elif is_regular(e):
            w.file(e.name, e.data, e.mode & 0o7777)
        else:
            raise InitrdError(f"unsupported entry in the initramfs: {e.name}")
    return w.finish_gzip()


def build(base_entries: list[cpio.Entry], modloop: SquashFS, kver: str,
          extra_modules: list[str], early_sh: bytes,
          modprobe_conf: bytes,
          background_modules: list[str] = (),
          keep_services: list[str] = ()) -> tuple[bytes, list[str]]:
    """Return (gzip'd cpio, list of modules added). `background_modules` are
    added too, and listed in /ingest/background-modules for early.sh."""
    names = {e.name: e for e in base_entries}
    init = names.get("init")
    if init is None:
        raise InitrdError("official initramfs has no /init")
    patched = patch_init(init.data.decode("utf-8"))

    # the official initramfs is usr-merged (lib -> usr/lib)
    moddir = f"usr/lib/modules/{kver}"
    if f"{moddir}/modules.dep" not in names:
        raise InitrdError(f"official initramfs has no {moddir}/modules.dep")

    src = f"/modules/{kver}"
    modules_dep = modloop.read_file(modloop.lookup(f"{src}/modules.dep")).decode()

    w = cpio.Writer()
    w.file("init", patched.encode(), 0o755)
    w.file("ingest/early.sh", early_sh, 0o644)
    w.file("etc/modprobe.d/ingest-wyse.conf", modprobe_conf, 0o644)
    if keep_services:
        # read by early.sh: the other OpenRC scripts are moved away
        w.file("ingest/keep-services",
               "".join(f"{s}\n" for s in sorted(set(keep_services))).encode(), 0o644)
    if background_modules:
        w.file("ingest/background-modules",
               "".join(f"{m}\n" for m in background_modules).encode(), 0o644)
        # the boot media scan (nlplug-findfs) loads the drivers of every
        # device it sees, synchronously: keep it off these, early.sh loads
        # them by name (which ignores the blacklist). Initramfs only.
        w.file("etc/modprobe.d/ingest-background.conf",
               "".join(f"blacklist {m}\n" for m in background_modules).encode(), 0o644)

    added = []
    for rel in module_closure(modules_dep, list(extra_modules) + list(background_modules)):
        path = f"{moddir}/{rel}"
        if path in names:
            continue
        w.file(path, modloop.read_file(modloop.lookup(f"{src}/{rel}")))
        added.append(rel)

    # replace the module index with the full one from the modloop so that
    # modprobe can resolve the modules we added
    for name, ref in modloop.listdir(modloop.lookup(src)).items():
        if name.startswith("modules.") and not name.endswith(".modinfo"):
            ino = modloop.inode(ref)
            if ino.is_file:
                w.file(f"{moddir}/{name}", modloop.read_file(ino))

    return w.finish_gzip(), added


def initramfs_kernel_version(entries: list[cpio.Entry]) -> str:
    kvers = {e.name.split("/")[3] for e in entries
             if e.name.startswith("usr/lib/modules/") and e.name.count("/") >= 4}
    if len(kvers) != 1:
        raise InitrdError(f"cannot determine kernel version from initramfs: {kvers}")
    return kvers.pop()


def is_regular(entry: cpio.Entry) -> bool:
    return stat.S_ISREG(entry.mode)
