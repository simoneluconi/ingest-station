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


# BOS descriptors read on the Wyse 3040
KINGSTON_AT_480 = bytes.fromhex("050f16000207100206000000" "0a1003000e00020aff07")
CANON_R6 = bytes.fromhex("050f58000507100206000000" "0a1003000e00010aff07"
                         "12100600544b0000000001000000200130011810")
USB2_ONLY = bytes.fromhex("050f0c000107100206000000")


def test_usb_superspeed_capable():
    # a USB 3 stick on a USB 2 link still lists the SuperSpeed capability
    assert devices.usb_superspeed_capable(KINGSTON_AT_480)
    assert devices.usb_superspeed_capable(CANON_R6)
    assert not devices.usb_superspeed_capable(USB2_ONLY)
    assert not devices.usb_superspeed_capable(b"")
    # a truncated or zero-length descriptor must not loop forever
    assert not devices.usb_superspeed_capable(bytes.fromhex("050f0c0001" "00"))


@pytest.mark.skipif(os.name == "nt", reason="sysfs symlinks")
def test_usb_link(tmp_path, monkeypatch):
    memory = tmp_path / "usb3-ports"
    monkeypatch.setattr(devices, "SS_PORTS", devices.SuperSpeedPorts(str(memory)))

    def port(name, peer=None):
        p = tmp_path / name
        p.mkdir()
        if peer:
            (p / "peer").symlink_to(tmp_path / peer)
        return p
    # two connectors with a SuperSpeed peer in sysfs; the second is wired
    # for USB 2 only (as on the Wyse 3040), which sysfs cannot tell
    port("usb2-port1")
    port("usb2-port2")
    front1 = port("usb1-port1", "usb2-port1")
    front2 = port("usb1-port2", "usb2-port2")
    usb2_only = port("usb1-port4")

    def device(name, speed, bos, at):
        d = tmp_path / name
        d.mkdir()
        (d / "speed").write_text(f"{speed}\n")
        (d / "bos_descriptors").write_bytes(bos)
        (d / "port").symlink_to(at)
        return str(d)
    # nothing learnt yet: a USB 3 stick at 480 is not flagged
    assert devices.usb_link(device("1-1", 480, KINGSTON_AT_480, front1)) == (480, False)
    # a USB 3 link on the first connector teaches that it carries USB 3
    assert devices.usb_link(device("2-1", 5000, KINGSTON_AT_480,
                                   tmp_path / "usb2-port1")) == (5000, False)
    assert memory.read_text() == "usb2-port1\n"
    # now a USB 3 stick at 480 there is a slow link (cable, contacts)...
    assert devices.usb_link(str(tmp_path / "1-1")) == (480, True)
    # ...but not on the connector that never carried USB 3
    assert devices.usb_link(device("1-2", 480, KINGSTON_AT_480, front2)) == (480, False)
    # nor in a USB 2-only port, nor for a USB 2 device
    assert devices.usb_link(device("1-4", 480, KINGSTON_AT_480, usb2_only)) == (480, False)
    assert devices.usb_link(device("1-5", 480, USB2_ONLY, front1)) == (480, False)
    # the memory survives a restart
    assert devices.SuperSpeedPorts(str(memory)).ports == {"usb2-port1"}
    assert devices.usb_link(None) == (0, False)


def test_read_back_ok(tmp_path, monkeypatch):
    cfg = {"subdir": ""}
    # a share that reads back what was written
    assert share.read_back_ok(cfg, str(tmp_path))
    assert not any(p.name.startswith(".readback") for p in (tmp_path / share.MARK_DIR).iterdir())
    # reads fail (an NFS version the server implements badly): try the next one
    real_read = os.read

    def bad_read(fd, n):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(share.os, "read", bad_read)
    assert not share.read_back_ok(cfg, str(tmp_path))
    monkeypatch.setattr(share.os, "read", real_read)
    # cannot write at all: not a version problem, check() reports it
    def no_write(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(share.os, "makedirs", no_write)
    assert share.read_back_ok(cfg, str(tmp_path))


def test_reachable_nfs(monkeypatch):
    cfg = {"type": "nfs", "server": "nas", "path": "/photos"}
    tries = share.candidates(cfg)

    def ports(**states):
        monkeypatch.setattr(share, "port_state", lambda host, port: states[f"p{port}"])
    # NFS 4 only (port 111 silent, as on a server without rpcbind)
    ports(p2049="open", p111="silent")
    skipped = []
    assert [t[3] for t in share._reachable_nfs(cfg, tries, skipped)] == \
        ["NFS 4.2", "NFS 4.1", "NFS 4.0"]
    assert skipped == ["NFS 3 is not offered by nas (no rpcbind on port 111)"]
    # forced NFS 3 on that server: a clear message, at once
    with pytest.raises(share.ShareError, match="NFS 3 is not offered"):
        share._reachable_nfs(cfg, share.candidates({**cfg, "version": "3"}), [])
    # nothing answers: not reachable; an unknown name says so
    ports(p2049="silent", p111="silent")
    with pytest.raises(share.ShareError, match="is not reachable"):
        share._reachable_nfs(cfg, tries, [])
    ports(p2049="unknown-host", p111="unknown-host")
    with pytest.raises(share.ShareError, match="cannot find 'nas'"):
        share._reachable_nfs(cfg, tries, [])
    # the host answers but offers no NFS at all
    ports(p2049="closed", p111="closed")
    with pytest.raises(share.ShareError, match="does not offer NFS"):
        share._reachable_nfs(cfg, tries, [])


def test_port_state():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert share.port_state("127.0.0.1", port) == "open"
    srv.close()
    assert share.port_state("127.0.0.1", port, timeout=1) in ("closed", "silent")
    assert share.port_state("no-such-host.invalid", 2049) == "unknown-host"
