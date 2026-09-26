import os

import pytest

from ingest.daemon import devices, share

from sysfs_fixture import disk


def usb_reader(root, slots):
    """A multi-slot USB card reader: one SCSI disk per slot below one USB
    device (sysfs uses symlinks /sys/block/sdX -> ../devices/...)."""
    usb = root / "devices" / "pci0000:00" / "0000:00:14.0" / "usb1" / "1-1"
    usb.mkdir(parents=True)
    (usb / "idVendor").write_text("05e3\n")
    (usb / "serial").write_bytes(b"000000001532\x92\n")   # not UTF-8, on purpose
    (root / "block").mkdir(exist_ok=True)
    for lun, (name, sectors, model, parts) in enumerate(slots):
        dev = usb / "1-1:1.0" / "host6" / "target6:0:0" / f"6:0:0:{lun}"
        blk = dev / "block" / name
        blk.mkdir(parents=True)
        (blk / "size").write_text(f"{sectors}\n")
        (blk / "removable").write_text("1\n")
        (dev / "model").write_text(f"{model}\n")
        (dev / "vendor").write_text("Generic \n")
        # like sysfs: block/<disk>/device -> the SCSI device (its name holds the LUN)
        os.symlink(dev, blk / "device", target_is_directory=True)
        for i, p in enumerate(parts, 1):
            (blk / p).mkdir()
            (blk / p / "partition").write_text(f"{i}\n")
            (blk / p / "size").write_text(f"{sectors - 2048}\n")
        os.symlink(blk, root / "block" / name, target_is_directory=True)


def test_card_reader_slots(tmp_path):
    try:
        usb_reader(tmp_path, [("sdb", 124735488, "SD/MMC", ["sdb1"]),
                              ("sdc", 0, "CF", []),
                              ("sdd", 62333952, "MS", [])])
    except OSError:
        pytest.skip("symlinks not available on this system")
    disks = {d.name: d for d in devices.scan_disks(str(tmp_path))}
    assert set(disks) == {"sdb", "sdc", "sdd"}
    assert disks["sdb"].usb_port == "1-1" and disks["sdb"].has_media
    assert not disks["sdc"].has_media                       # empty slot
    assert disks["sdb"].lun != disks["sdd"].lun            # one slot = one disk
    assert disks["sdb"].serial == "000000001532�"     # tolerant decoding
    assert devices.describe(disks["sdb"]) == "SD/MMC"


def test_scan_ignores_non_removable_non_usb(tmp_path):
    disk(str(tmp_path), "sda", 1000, removable=0)           # e.g. a SATA disk
    disk(str(tmp_path), "sdb", 1000, removable=1)
    disk(str(tmp_path), "mmcblk0", 15269888, dev_type="MMC")
    assert [d.name for d in devices.scan_disks(str(tmp_path))] == ["sdb"]


def test_volumes_partitioned_and_superfloppy(tmp_path):
    probes = {"/dev/sdb1": {"TYPE": "exfat", "LABEL": "EOS_DIGITAL", "UUID": "1234-ABCD"},
              "/dev/sdc": {"TYPE": "vfat", "LABEL": "NO NAME"},
              "/dev/sdd1": {"TYPE": "apfs"}, "/dev/sdd2": {}}
    d1 = devices.Disk("sdb", "x", 10**9, partitions=["sdb1"])
    d2 = devices.Disk("sdc", "y", 10**9)                   # no partition table
    d3 = devices.Disk("sdd", "z", 10**9, partitions=["sdd1", "sdd2"])
    v1 = devices.volumes(d1, str(tmp_path), probes.get)
    assert [(v.dev, v.fstype, v.label) for v in v1] == [("/dev/sdb1", "exfat", "EOS_DIGITAL")]
    assert [v.dev for v in devices.volumes(d2, str(tmp_path), probes.get)] == ["/dev/sdc"]
    v3 = devices.volumes(d3, str(tmp_path), lambda dev: probes.get(dev, {}))
    assert [v.fstype for v in v3] == ["apfs"]
    assert devices.mount_type("apfs") is None
    assert "Mac" in devices.unsupported_reason("apfs")
    assert devices.mount_type("ntfs") == "ntfs3" and devices.mount_type("exfat") == "exfat"


def test_blkid_and_uevent_parsing(tmp_path):
    assert devices.parse_blkid_export("DEVNAME=/dev/sdb1\nLABEL=My Card\nTYPE=exfat\n") == {
        "DEVNAME": "/dev/sdb1", "LABEL": "My Card", "TYPE": "exfat"}
    raw = b"change@/devices/.../block/sdb\0ACTION=change\0SUBSYSTEM=block\0DISK_MEDIA_CHANGE=1\0"
    ev = devices.parse_uevent(raw)
    assert ev["SUBSYSTEM"] == "block" and ev["DISK_MEDIA_CHANGE"] == "1"
    (tmp_path / "ingest-update-v0.2.0.pkg").write_bytes(b"x")
    assert devices.is_update_media(str(tmp_path))


def test_share_candidates():
    nfs = share.candidates({"type": "nfs", "server": "nas", "path": "volume1/photos"})
    assert [c[3] for c in nfs] == ["NFS 4.2", "NFS 4.1", "NFS 4.0", "NFS 3"]
    assert nfs[0][1] == "nas:/volume1/photos" and "soft" in nfs[0][2]
    assert nfs[-1][0] == "nfs" and "nolock" in nfs[-1][2]
    smb = share.candidates({"type": "smb", "server": "nas", "path": "\\Photos",
                            "version": "2.1"})
    assert smb == [("cifs", "//nas/Photos", smb[0][2], "SMB 2.1")]
    assert "vers=2.1" in smb[0][2] and "guest" in smb[0][2]
    assert share.dest_root({"subdir": "Ingest/2026"}, "/mnt") == os.path.join("/mnt", "Ingest",
                                                                              "2026")
