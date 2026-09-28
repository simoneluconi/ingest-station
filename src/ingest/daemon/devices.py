"""Source devices: USB disks, card readers (one disk per slot, empty slot =
disk without media), sticks, external drives.

Discovery is a sysfs snapshot, refreshed on every kernel uevent (netlink)
and every couple of seconds as a safety net: card readers only report media
changes through the kernel's periodic polling (block.events_dfl_poll_msecs).
All strings are decoded tolerantly.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field

from ingest.common import sysfs

SUPPORTED_FS = {"vfat": "vfat", "exfat": "exfat", "ntfs": "ntfs3", "ntfs3": "ntfs3",
                "ext4": "ext4", "ext3": "ext4", "ext2": "ext4", "hfsplus": "hfsplus"}
UNSUPPORTED_MSG = {
    "apfs": "APFS (Mac) is not supported: copy the files on a Mac or reformat as exFAT",
    "hfs": "HFS (old Mac format) is not supported",
    "crypto_LUKS": "Encrypted disk: not supported",
    "BitLocker": "BitLocker encrypted disk: not supported",
}
UPDATE_GLOB = re.compile(r"^ingest-update-.*\.pkg$")


@dataclass
class Disk:
    """A block device on a removable bus (one per card reader slot)."""
    name: str                 # sdb
    devpath: str              # stable sysfs path, identifies the slot/port
    size: int
    vendor: str = ""
    model: str = ""
    serial: str = ""
    usb_port: str = ""        # e.g. 1-1.2
    lun: str = ""
    partitions: list[str] = field(default_factory=list)
    usb_speed: int = 0        # negotiated link speed, Mbit/s (480, 5000...)
    usb3: bool = False        # the device supports USB 3 (SuperSpeed)

    @property
    def has_media(self) -> bool:
        return self.size > 0


@dataclass
class Volume:
    """A file system on a disk (a partition, or the whole disk)."""
    dev: str                  # /dev/sdb1
    fstype: str
    label: str
    uuid: str
    size: int


def _usb_ancestor(real: str) -> str | None:
    """Sysfs directory of the USB device holding a block device."""
    path = real
    while True:
        if os.path.exists(os.path.join(path, "idVendor")):
            return path
        parent = os.path.dirname(path)
        if parent == path:   # reached the root
            return None
        path = parent


def usb_superspeed_capable(bos: bytes) -> bool:
    """True if a BOS descriptor set lists a SuperSpeed (USB 3) capability.
    A USB 3 device running at USB 2 speed reports bcdUSB 2.10, so the
    version alone cannot tell; the BOS capabilities can."""
    i = bos[0] if bos else 0          # skip the BOS header descriptor
    while i + 2 < len(bos) and bos[i] > 0:
        if bos[i + 1] == 0x10 and bos[i + 2] in (0x03, 0x0A):   # SuperSpeed, SuperSpeedPlus
            return True
        i += bos[i]
    return False


def usb_link(usb_dir: str | None) -> tuple[int, bool]:
    """(negotiated speed in Mbit/s, USB 3 capable) of a USB device directory
    in sysfs; (0, False) when unknown."""
    if not usb_dir:
        return 0, False
    try:
        speed = int(float(sysfs.read_text(f"{usb_dir}/speed", "0")))
    except ValueError:
        speed = 0
    try:
        with open(f"{usb_dir}/bos_descriptors", "rb") as f:
            capable = usb_superspeed_capable(f.read())
    except OSError:
        capable = speed >= 5000
    return speed, capable


def scan_disks(sys_root: str = "/sys", exclude: set[str] | None = None) -> list[Disk]:
    """USB/removable disks (not the system eMMC). Includes empty reader slots."""
    exclude = exclude or set()
    out = []
    base = f"{sys_root}/block"
    for name in sysfs.listdir(base):
        if not name.startswith("sd") or name in exclude:
            continue
        d = f"{base}/{name}"
        real = os.path.realpath(d)
        usb = _usb_ancestor(real)
        removable = sysfs.read_int(f"{d}/removable") == 1
        if usb is None and not removable:
            continue
        parts = [p for p in sysfs.listdir(d)
                 if p.startswith(name) and os.path.exists(f"{d}/{p}/partition")]
        dev = f"{d}/device"
        speed, usb3 = usb_link(usb)
        out.append(Disk(
            name=name,
            devpath=real.replace(sys_root, "", 1) if real.startswith(sys_root) else real,
            size=sysfs.read_int(f"{d}/size") * 512,
            vendor=sysfs.read_text(f"{dev}/vendor"),
            model=sysfs.read_text(f"{dev}/model"),
            serial=sysfs.read_text(f"{usb}/serial") if usb else "",
            usb_port=os.path.basename(usb) if usb else "",
            lun=os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else "",
            partitions=parts,
            usb_speed=speed,
            usb3=usb3,
        ))
    return out


def parse_blkid_export(text: str) -> dict[str, str]:
    """`blkid -o export` output -> dict (values decoded tolerantly)."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def probe(dev: str) -> dict[str, str]:
    """File system type/label/uuid of a block device (low-level probe, so
    stale caches never lie about a freshly inserted card)."""
    try:
        return parse_blkid_export(sysfs.run(["blkid", "-p", "-o", "export", dev],
                                            check=False, timeout=20))
    except Exception:
        return {}


def volumes(disk: Disk, sys_root: str = "/sys", prober=probe) -> list[Volume]:
    """File systems of a disk: its partitions, or the disk itself when it
    has no partition table (common on cards formatted by cameras)."""
    names = disk.partitions or [disk.name]
    out = []
    for n in names:
        info = prober(f"/dev/{n}")
        fstype = info.get("TYPE", "")
        if not fstype:
            continue
        size = sysfs.read_int(f"{sys_root}/class/block/{n}/size") * 512 or disk.size
        out.append(Volume(dev=f"/dev/{n}", fstype=fstype, label=info.get("LABEL", ""),
                          uuid=info.get("UUID", ""), size=size))
    return out


def mount_type(fstype: str) -> str | None:
    return SUPPORTED_FS.get(fstype)


def unsupported_reason(fstype: str) -> str:
    return UNSUPPORTED_MSG.get(fstype, f"file system '{fstype}' is not supported")


def is_update_media(mount_point: str) -> bool:
    return any(UPDATE_GLOB.match(n) for n in sysfs.listdir(mount_point))


def describe(disk: Disk) -> str:
    """Human readable name: 'Generic SD/MMC', 'SanDisk Extreme'..."""
    name = " ".join(x for x in (disk.vendor, disk.model) if x and x.upper() != "GENERIC")
    return name or disk.model or disk.name


# -- kernel uevents ----------------------------------------------------

NETLINK_KOBJECT_UEVENT = 15


def uevent_socket() -> socket.socket | None:
    """Non-blocking netlink socket receiving kernel uevents (Linux only)."""
    if not hasattr(socket, "AF_NETLINK"):
        return None
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
        s.bind((0, 1))
        s.setblocking(False)
        return s
    except OSError:
        return None


def parse_uevent(data: bytes) -> dict[str, str]:
    parts = data.split(b"\0")
    ev = {}
    for p in parts[1:]:
        k, sep, v = p.partition(b"=")
        if sep:
            ev[sysfs.decode(k)] = sysfs.decode(v)
    return ev
