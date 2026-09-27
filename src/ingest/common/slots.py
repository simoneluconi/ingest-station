"""A/B system slots on the ESP, driven through grubenv.

grubenv keys (see buildlib/boot/grub-system.cfg):
  active_slot  last known-good slot
  try_slot     slot on trial after an update; try_left attempts left
  try_failed   set by GRUB when a trial ran out of attempts
  data_uuid    UUID of the data partition

Also provides the `ingest-slot` command used on the device.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from . import blockdev, grubenv, sysfs

SLOTS = ("A", "B")
GRUBENV = "boot/grub/grubenv"
SLOT_MANIFEST = "slot.json"
STAGING = ".staging"


class SlotError(Exception):
    pass


def other(slot: str) -> str:
    return "B" if slot == "A" else "A"


def find_esp(mounts_path: str = "/proc/mounts") -> str | None:
    """Mount point of the installed ESP (holds grubenv and slots/)."""
    for _dev, mnt, fstype, _opts in blockdev.mounts(mounts_path):
        if fstype == "vfat" and os.path.isfile(os.path.join(mnt, GRUBENV)) \
                and os.path.isdir(os.path.join(mnt, "slots")):
            return mnt
    return None


def sha256_file(path: str, bufsize: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(slot_dir: str) -> dict | None:
    try:
        with open(os.path.join(slot_dir, SLOT_MANIFEST), "rb") as f:
            return json.loads(sysfs.decode(f.read()))
    except (OSError, ValueError):
        return None


def verify_slot(slot_dir: str) -> list[str]:
    """Check every file listed in slot.json; returns a list of problems."""
    man = read_manifest(slot_dir)
    if not man:
        return [f"{slot_dir}: missing or invalid {SLOT_MANIFEST}"]
    problems = []
    for rel, digest in man.get("files", {}).items():
        path = os.path.join(slot_dir, *rel.split("/"))
        if not os.path.isfile(path):
            problems.append(f"missing {rel}")
        elif sha256_file(path) != digest:
            problems.append(f"corrupt {rel}")
    return problems


def drop_caches() -> None:
    """Make the next reads come from the device, not the page cache."""
    with contextlib.suppress(OSError):
        sysfs.sync()
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")


class SlotManager:
    def __init__(self, esp: str, current: str | None, remount: bool = True):
        self.esp = esp
        self.current = current
        self.remount = remount
        self.env_path = os.path.join(esp, GRUBENV)

    def slot_dir(self, slot: str) -> str:
        return os.path.join(self.esp, "slots", slot)

    def env(self) -> dict[str, str]:
        return grubenv.read(self.env_path)

    @contextlib.contextmanager
    def writable(self):
        """Temporarily remount the ESP read-write."""
        if self.remount:
            subprocess.run(["mount", "-o", "remount,rw", self.esp], check=True)
        try:
            yield
        finally:
            if self.remount:
                sysfs.sync()
                subprocess.run(["mount", "-o", "remount,ro", self.esp], check=False)

    def _save(self, env: dict[str, str]) -> None:
        with self.writable():
            grubenv.write(self.env_path, env)

    def status(self) -> dict:
        env = self.env()
        slots = {}
        for s in SLOTS:
            man = read_manifest(self.slot_dir(s))
            slots[s] = {"present": man is not None,
                        "version": (man or {}).get("version", ""),
                        "kernel": (man or {}).get("kernel", "")}
        return {
            "current": self.current,
            "active": env.get("active_slot", "A"),
            "try_slot": env.get("try_slot", ""),
            "try_left": env.get("try_left", ""),
            "try_failed": env.get("try_failed", ""),
            "slots": slots,
        }

    def on_trial(self) -> bool:
        env = self.env()
        return bool(env.get("try_slot")) and env.get("try_slot") == self.current

    def mark_good(self) -> bool:
        """Confirm the running slot. Returns True if grubenv changed."""
        env = self.env()
        if env.get("try_slot") and env.get("try_slot") == self.current:
            env["active_slot"] = self.current
            env.pop("try_slot", None)
            env.pop("try_left", None)
            self._save(env)
            return True
        return False

    def start_trial(self, slot: str, tries: int = 3) -> None:
        if slot not in SLOTS:
            raise SlotError(f"invalid slot {slot!r}")
        if not 1 <= tries <= 3:
            raise SlotError("tries must be between 1 and 3")
        if read_manifest(self.slot_dir(slot)) is None:
            raise SlotError(f"slot {slot} does not contain a system")
        env = self.env()
        env["try_slot"] = slot
        env["try_left"] = str(tries)
        env.pop("try_failed", None)
        self._save(env)

    def set_active(self, slot: str) -> None:
        """Manual rollback/switch: make `slot` the known-good slot."""
        if slot not in SLOTS:
            raise SlotError(f"invalid slot {slot!r}")
        problems = verify_slot(self.slot_dir(slot))
        if problems:
            raise SlotError(f"slot {slot} is not usable: " + "; ".join(problems[:5]))
        env = self.env()
        env["active_slot"] = slot
        env.pop("try_slot", None)
        env.pop("try_left", None)
        self._save(env)

    def ack_failure(self) -> None:
        env = self.env()
        if env.pop("try_failed", None) is not None:
            self._save(env)

    def clone_current(self, log=print) -> None:
        """Copy the running slot to the other one (used to test A/B)."""
        if self.current not in SLOTS:
            raise SlotError("cannot tell which slot is running")
        src, dst = self.slot_dir(self.current), self.slot_dir(other(self.current))
        with self.writable():
            if os.path.exists(dst):
                shutil.rmtree(dst)
            for root, _dirs, files in os.walk(src):
                rel = os.path.relpath(root, src)
                os.makedirs(os.path.join(dst, rel), exist_ok=True)
                for name in files:
                    shutil.copyfile(os.path.join(root, name), os.path.join(dst, rel, name))
            sysfs.sync()
        drop_caches()
        problems = verify_slot(dst)
        if problems:
            raise SlotError("copy verification failed: " + "; ".join(problems[:5]))
        log(f"slot {self.current} copied to {other(self.current)} and verified")

    # -- updates: a new system is staged next to the slots, completed with
    #    the unchanged files of the running slot, verified, then swapped in
    #    as the inactive slot and booted on trial.

    def staging_dir(self) -> str:
        return os.path.join(self.esp, "slots", STAGING)

    def stage_begin(self) -> dict:
        """Prepare an empty staging area (leaves the ESP read-write).
        Returns what the sender needs: target slot, staging path and the
        manifest of the running slot (to send only changed files)."""
        if self.current not in SLOTS:
            raise SlotError("cannot tell which slot is running")
        env = self.env()
        if env.get("try_slot"):
            raise SlotError(f"slot {env['try_slot']} is still on trial: reboot and let it "
                            "confirm (or roll back) before sending an update")
        if self.remount:
            subprocess.run(["mount", "-o", "remount,rw", self.esp], check=True)
        staging = self.staging_dir()
        if os.path.exists(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)
        return {"current": self.current, "target": other(self.current), "staging": staging,
                "manifest": read_manifest(self.slot_dir(self.current)) or {}}

    def stage_abort(self) -> None:
        staging = self.staging_dir()
        with self.writable():
            if os.path.exists(staging):
                shutil.rmtree(staging)

    def stage_commit(self, tries: int = 3, log=print) -> list[str]:
        """Complete, verify and activate the staged system on trial.
        Returns the bootloader files that were updated on the ESP."""
        staging = self.staging_dir()
        new = read_manifest(staging)
        if not new:
            raise SlotError(f"nothing staged ({SLOT_MANIFEST} missing)")
        if self.current not in SLOTS:
            raise SlotError("cannot tell which slot is running")
        running = self.slot_dir(self.current)
        running_files = (read_manifest(running) or {}).get("files", {})
        target = other(self.current)
        with self.writable():
            reused = 0
            for rel, digest in new.get("files", {}).items():
                dst = os.path.join(staging, *rel.split("/"))
                if os.path.isfile(dst):
                    continue
                src = os.path.join(running, *rel.split("/"))
                if running_files.get(rel) != digest or not os.path.isfile(src):
                    raise SlotError(f"{rel} was not sent and is not in the running slot")
                copy_file(src, dst)
                reused += 1
            sysfs.sync()
            drop_caches()
            problems = verify_slot(staging)
            if problems:
                raise SlotError("staged system is damaged: " + "; ".join(problems[:5]))
            log(f"staged system verified ({reused} files reused from slot {self.current})")
            updated = install_bootloader(staging, self.esp)
            target_dir = self.slot_dir(target)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            os.rename(staging, target_dir)
            sysfs.sync()
            env = self.env()
            env["try_slot"] = target
            env["try_left"] = str(tries)
            env.pop("try_failed", None)
            grubenv.write(self.env_path, env)
        log(f"slot {target} ({new.get('version', '?')}) will boot on trial ({tries} attempts)")
        return updated


def copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
        fo.flush()
        os.fsync(fo.fileno())


def install_bootloader(slot_dir: str, esp: str) -> list[str]:
    """Copy the slot's bootloader files (boot/) to their places on the ESP,
    only where they differ. The bootloader is shared by both slots, so it is
    replaced file by file through a temporary name. Returns the ESP paths
    that were updated."""
    boot = os.path.join(slot_dir, "boot")
    pairs = [(os.path.join(boot, "BOOTX64.EFI"), os.path.join(esp, "EFI", "BOOT", "BOOTX64.EFI")),
             (os.path.join(boot, "grub.cfg"), os.path.join(esp, "boot", "grub", "grub.cfg"))]
    for mod in sysfs.listdir(os.path.join(boot, "x86_64-efi")):
        pairs.append((os.path.join(boot, "x86_64-efi", mod),
                      os.path.join(esp, "boot", "grub", "x86_64-efi", mod)))
    updated = []
    for src, dst in pairs:
        if os.path.isfile(dst) and sha256_file(dst) == sha256_file(src):
            continue
        tmp = dst + ".new"
        copy_file(src, tmp)
        os.replace(tmp, dst)
        updated.append(os.path.relpath(dst, esp).replace(os.sep, "/"))
    return updated


# --------------------------------------------------------------------------
# health check used by `ingest-slot boot-ok`

def health_problems(cmdline: dict[str, str]) -> list[str]:
    problems = []
    if cmdline.get("ingest_data", "").removeprefix("UUID=") and not os.path.ismount("/data"):
        problems.append("data partition not mounted")
    try:
        from .client import Client
        with Client(timeout=10) as c:
            if c.call("ping") != "pong":
                problems.append("ingestd does not answer")
    except Exception as e:  # noqa: BLE001 - any failure means unhealthy
        problems.append(f"ingestd: {e}")
    # the GUI must be running too (it writes a heartbeat every 5 s)
    if os.path.exists("/etc/runlevels/default/ingest-gui"):
        try:
            age = time.time() - os.path.getmtime("/run/ingest-gui.alive")
        except OSError:
            age = None
        if age is None or age > 20:
            problems.append("the GUI is not running")
    return problems


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with contextlib.suppress(OSError):
        with open("/data/log/boot.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def cmd_boot_ok(mgr: SlotManager, cmdline: dict[str, str], wait: int) -> int:
    deadline = time.monotonic() + wait
    while True:
        problems = health_problems(cmdline)
        if not problems:
            changed = mgr.mark_good()
            uptime = sysfs.read_text("/proc/uptime", "0").split()[0]
            _log(f"boot OK on slot {mgr.current} at {uptime} s after kernel start"
                 + (" (update confirmed)" if changed else ""))
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    _log(f"boot NOT healthy on slot {mgr.current}: {'; '.join(problems)}")
    if mgr.on_trial():
        _log("slot is on trial: rebooting so GRUB can count the attempt")
        subprocess.run(["reboot"], check=False)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingest-slot", description="Manage the A/B system slots")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show slot state")
    b = sub.add_parser("boot-ok", help="confirm this boot once healthy")
    b.add_argument("--wait", type=int, default=180)
    t = sub.add_parser("try", help="boot SLOT on trial from the next reboot")
    t.add_argument("slot", choices=SLOTS)
    t.add_argument("--tries", type=int, default=3)
    sub.add_parser("rollback", help="make the other slot the active one")
    a = sub.add_parser("set-active", help="make SLOT the active one")
    a.add_argument("slot", choices=SLOTS)
    sub.add_parser("clone", help="copy the running slot to the other slot")
    sub.add_parser("ack", help="clear the 'update rolled back' flag")
    sub.add_parser("stage-begin", help="(update) prepare the staging area, print JSON")
    c = sub.add_parser("stage-commit", help="(update) verify the staged system, boot it on trial")
    c.add_argument("--tries", type=int, default=3)
    c.add_argument("--reboot", action="store_true")
    sub.add_parser("stage-abort", help="(update) discard the staging area")
    args = p.parse_args(argv)

    esp = find_esp()
    if not esp:
        print("ingest-slot: installed ESP not found (not booted from eMMC?)", file=sys.stderr)
        return 2
    cmdline = sysfs.kernel_cmdline()
    mgr = SlotManager(esp, cmdline.get("ingest_slot"))
    try:
        if args.cmd == "status":
            print(json.dumps(mgr.status(), indent=2))
        elif args.cmd == "boot-ok":
            return cmd_boot_ok(mgr, cmdline, args.wait)
        elif args.cmd == "try":
            mgr.start_trial(args.slot, args.tries)
            print(f"slot {args.slot} will boot on trial ({args.tries} attempts); reboot to start")
        elif args.cmd == "rollback":
            target = other(mgr.env().get("active_slot", "A"))
            mgr.set_active(target)
            print(f"slot {target} is now active; reboot to use it")
        elif args.cmd == "set-active":
            mgr.set_active(args.slot)
            print(f"slot {args.slot} is now active; reboot to use it")
        elif args.cmd == "clone":
            mgr.clone_current()
        elif args.cmd == "ack":
            mgr.ack_failure()
        elif args.cmd == "stage-begin":
            print(json.dumps(mgr.stage_begin()))
        elif args.cmd == "stage-commit":
            updated = mgr.stage_commit(args.tries)
            if updated:
                print("bootloader files updated: " + ", ".join(updated))
            if args.reboot:
                print("rebooting", flush=True)
                subprocess.Popen(["sh", "-c", "sleep 2; reboot"], start_new_session=True)
        elif args.cmd == "stage-abort":
            mgr.stage_abort()
    except (SlotError, grubenv.GrubEnvError, OSError) as e:
        print(f"ingest-slot: {e}", file=sys.stderr)
        return 1
    return 0
