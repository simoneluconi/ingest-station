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


# --------------------------------------------------------------------------
# health check used by `ingest-slot boot-ok`

def health_problems(cmdline: dict[str, str]) -> list[str]:
    problems = []
    if cmdline.get("ingest_data", "").removeprefix("UUID=") and not os.path.ismount("/data"):
        problems.append("data partition not mounted")
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
    except (SlotError, grubenv.GrubEnvError) as e:
        print(f"ingest-slot: {e}", file=sys.stderr)
        return 1
    return 0
