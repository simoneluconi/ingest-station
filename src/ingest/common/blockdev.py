"""Block device discovery from sysfs, shared by the installer and the daemon.

Never relies on fixed device names. All strings are decoded tolerantly
(see sysfs.py); the installer does not read model names at all.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import sysfs

MIN_INSTALL_BYTES = 4 * 1024**3
_MMC_HW_PART = re.compile(r"^mmcblk\d+(boot\d+|rpmb)$")
_VIRTUAL = re.compile(r"^(loop|ram|zram|nbd|dm-|md)\d*")


@dataclass
class Disk:
    name: str
    size: int
    removable: bool
    read_only: bool
    dev_type: str
    partitions: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def size_gb(self) -> float:
        return self.size / 1e9

    def describe(self) -> str:
        flags = []
        if self.removable:
            flags.append("removable")
        if self.read_only:
            flags.append("read-only")
        kind = self.dev_type or "?"
        extra = f" [{', '.join(flags)}]" if flags else ""
        model = f" {self.model}" if self.model else ""
        return f"{self.name}: {self.size_gb:.1f} GB, type {kind}{model}{extra}"


def partition_name(disk: str, number: int) -> str:
    """mmcblk0 -> mmcblk0p1, nvme0n1 -> nvme0n1p1, sda -> sda1."""
    return f"{disk}{'p' if disk[-1].isdigit() else ''}{number}"


def list_disks(sys_root: str = "/sys", read_model: bool = False) -> list[Disk]:
    base = f"{sys_root}/block"
    disks = []
    for name in sysfs.listdir(base):
        if _VIRTUAL.match(name):
            continue
        d = f"{base}/{name}"
        parts = [p for p in sysfs.listdir(d)
                 if p.startswith(name) and os.path.exists(f"{d}/{p}/partition")]
        model = ""
        if read_model:
            model = (sysfs.read_text(f"{d}/device/model")
                     or sysfs.read_text(f"{d}/device/name"))
        disks.append(Disk(
            name=name,
            size=sysfs.read_int(f"{d}/size") * 512,
            removable=sysfs.read_int(f"{d}/removable") == 1,
            read_only=(sysfs.read_int(f"{d}/ro") == 1
                       or sysfs.read_int(f"{d}/force_ro") == 1),
            dev_type=sysfs.read_text(f"{d}/device/type"),
            partitions=parts,
            model=model,
        ))
    return disks


def parent_disk(name: str, sys_root: str = "/sys") -> str:
    """Disk that holds partition `name` (or `name` itself if it is a disk)."""
    base = f"{sys_root}/block"
    if os.path.isdir(f"{base}/{name}"):
        return name
    for disk in sysfs.listdir(base):
        if os.path.isdir(f"{base}/{disk}/{name}"):
            return disk
    return name


def install_candidates(disks: list[Disk], exclude: set[str]) -> tuple[list[Disk], list[tuple[Disk, str]]]:
    """Pick the internal eMMC: device/type == MMC, not removable, not one of
    the eMMC hardware partitions (boot0/boot1/rpmb) and not the boot stick.
    Returns (candidates, [(rejected disk, reason)])."""
    ok, rejected = [], []
    for d in disks:
        if _MMC_HW_PART.match(d.name):
            rejected.append((d, "eMMC hardware partition"))
        elif d.name in exclude:
            rejected.append((d, "boot device"))
        elif not d.name.startswith("mmcblk"):
            rejected.append((d, "not an MMC block device"))
        elif d.dev_type != "MMC":
            rejected.append((d, f"device type is {d.dev_type or 'unknown'}, not MMC"))
        elif d.removable:
            rejected.append((d, "removable"))
        elif d.size < MIN_INSTALL_BYTES:
            rejected.append((d, f"too small ({d.size_gb:.1f} GB)"))
        else:
            ok.append(d)
    return ok, rejected


def mounts(path: str = "/proc/mounts") -> list[tuple[str, str, str, str]]:
    """(device, mountpoint, fstype, options); octal escapes decoded."""
    out = []
    for line in sysfs.read_text(path).splitlines():
        f = line.split()
        if len(f) >= 4:
            dev, mnt = (re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), x)
                        for x in f[:2])
            out.append((dev, mnt, f[2], f[3]))
    return out
