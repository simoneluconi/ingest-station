"""Build fake /sys trees for block device tests."""

import os


def _w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else str(data).encode() + b"\n")


def disk(root, name, sectors, removable=0, ro=0, dev_type=None, model=None,
         name_attr=None, partitions=(), force_ro=None):
    d = os.path.join(root, "block", name)
    _w(os.path.join(d, "size"), sectors)
    _w(os.path.join(d, "removable"), removable)
    _w(os.path.join(d, "ro"), ro)
    if force_ro is not None:
        _w(os.path.join(d, "force_ro"), force_ro)
    if dev_type is not None:
        _w(os.path.join(d, "device", "type"), dev_type)
    if model is not None:
        _w(os.path.join(d, "device", "model"), model)
    if name_attr is not None:
        _w(os.path.join(d, "device", "name"), name_attr)
    for i, p in enumerate(partitions, 1):
        _w(os.path.join(d, p, "partition"), i)
        _w(os.path.join(d, p, "size"), 2048)


def wyse_3040(root, with_stick=True):
    """Internal 8 GB eMMC with the non-UTF-8 model name, its hardware
    partitions, and optionally the USB installer stick."""
    disk(root, "mmcblk0", 15269888, dev_type="MMC", name_attr=b"MMC H8G4a\x92\n",
         partitions=["mmcblk0p1", "mmcblk0p2", "mmcblk0p3"])
    disk(root, "mmcblk0boot0", 8192, ro=1, force_ro=1, dev_type="MMC")
    disk(root, "mmcblk0boot1", 8192, ro=1, force_ro=1, dev_type="MMC")
    disk(root, "mmcblk0rpmb", 8192, dev_type="MMC")
    disk(root, "loop0", 600000)
    if with_stick:
        disk(root, "sda", 60062500, removable=1, model=b"Ultra Fit       ",
             partitions=["sda1"])
