"""Read-only mounting of sources. The station never writes to a source:
the block device itself is set read-only in the kernel before mounting, and
ext4 is mounted with `noload` so not even its journal is replayed."""

from __future__ import annotations

import os
import subprocess

from ingest.common import blockdev, sysfs

SRC_ROOT = "/run/ingest/src"

OPTIONS = {
    "vfat": "ro,noatime,iocharset=utf8,shortname=mixed",
    "exfat": "ro,noatime,iocharset=utf8",
    "ntfs3": "ro,noatime,iocharset=utf8",
    "ext4": "ro,noatime,noload",
    "hfsplus": "ro,noatime",
}


class MountError(Exception):
    pass


def mount_point(key: str) -> str:
    return os.path.join(SRC_ROOT, key)


def mount_ro(dev: str, fstype: str, key: str) -> str:
    mnt = mount_point(key)
    os.makedirs(mnt, exist_ok=True)
    if os.path.ismount(mnt):
        return mnt
    subprocess.run(["blockdev", "--setro", dev], check=False, capture_output=True)
    res = subprocess.run(["mount", "-t", fstype, "-o", OPTIONS.get(fstype, "ro,noatime"),
                          dev, mnt], capture_output=True, timeout=60)
    if res.returncode != 0:
        raise MountError(sysfs.decode(res.stderr or res.stdout).strip() or "mount failed")
    return mnt


def umount(mnt: str, lazy: bool = False) -> bool:
    if not os.path.ismount(mnt):
        return True
    cmd = ["umount", "-l", mnt] if lazy else ["umount", mnt]
    ok = subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    if ok:
        try:
            os.rmdir(mnt)
        except OSError:
            pass
    return ok


def cleanup_stale() -> None:
    """At daemon start: unmount leftovers of a previous run."""
    for _dev, mnt, _t, _o in blockdev.mounts():
        if mnt.startswith(SRC_ROOT + "/"):
            umount(mnt, lazy=True)
