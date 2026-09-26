import ctypes
import functools
import os
import sys
import threading

import pytest

from ingest.daemon import copier, importer, ptp, scanner
from ingest.daemon.index import Index


def test_struct_layouts_match_libgphoto2_2_5_33():
    assert ctypes.sizeof(ptp.CameraAbilities) == 2504      # passed by value
    assert ctypes.sizeof(ptp.CameraStorageInformation) == 808
    assert ctypes.sizeof(ptp.CameraFileInfo) == 272
    assert ptp.CameraFileInfoFile.mtime.offset == 96
    assert ptp.CameraStorageInformation.capacitykbytes.offset == 784


@pytest.mark.skipif(sys.platform == "win32", reason="sysfs names contain ':'")
def test_usb_cameras_from_sysfs(tmp_path):
    base = tmp_path / "bus" / "usb" / "devices"
    cam = base / "1-2"
    (cam / "1-2:1.0").mkdir(parents=True)
    (cam / "1-2:1.0" / "bInterfaceClass").write_text("06\n")
    for f, v in {"busnum": "1", "devnum": "7", "serial": "123456", "manufacturer": "Canon Inc.",
                 "product": "Canon Digital Camera"}.items():
        (cam / f).write_text(v + "\n")
    phone = base / "1-3"
    (phone / "1-3:1.0").mkdir(parents=True)
    (phone / "1-3:1.0" / "bInterfaceClass").write_text("ff\n")
    (phone / "1-3:1.0" / "interface").write_text("MTP\n")
    for f, v in {"busnum": "1", "devnum": "9"}.items():
        (phone / f).write_text(v + "\n")
    stick = base / "2-1"
    (stick / "2-1:1.0").mkdir(parents=True)
    (stick / "2-1:1.0" / "bInterfaceClass").write_text("08\n")   # mass storage
    found = ptp.usb_cameras(str(tmp_path))
    assert [(c.port, c.serial) for c in found] == [("usb:001,007", "123456"), ("usb:001,009", "")]


class FakeCamera:
    """Serves files from memory through partial reads, like PTP."""

    def __init__(self, files):
        self.files = files
        self.reads = 0

    def read(self, folder, name, offset, buf):
        data = self.files[f"{folder}/{name}"]
        chunk = data[offset:offset + len(buf)]
        memoryview(buf)[:len(chunk)] = chunk
        self.reads += 1
        return len(chunk)


def camera_items(cam, base="/store_00010001"):
    items = []
    for path, data in sorted(cam.files.items()):
        folder, name = path.rsplit("/", 1)
        cf = ptp.CamFile(folder, name, len(data), 1_700_000_000.0)
        items.append(copier.Item(src=path, rel=path[len(base) + 1:], size=len(data),
                                 mtime=cf.mtime,
                                 opener=functools.partial(ptp.CameraStream, cam, cf)))
    return items


def test_camera_stream_partial_reads():
    data = os.urandom(3 * 1024 * 1024 + 11)
    cam = FakeCamera({"/s/DCIM/A.CR3": data})
    st = ptp.CameraStream(cam, ptp.CamFile("/s/DCIM", "A.CR3", len(data), 0))
    buf = bytearray(1 << 20)
    out = bytearray()
    while n := st.readinto(memoryview(buf)[:777_777]):
        out += buf[:n]
    assert bytes(out) == data


def test_import_from_camera_and_cross_recognition(tmp_path):
    files = {f"/store_00010001/DCIM/100CANON/IMG_{i:04d}.CR3": os.urandom(2_500_000 + i)
             for i in range(4)}
    cam = FakeCamera(files)
    idx = Index(str(tmp_path / "idx.db"))
    # the same pictures were imported from a card reader first
    card = tmp_path / "card"
    first = list(files.items())[:2]
    for path, data in first:
        p = card / path.split("/", 2)[2]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    share = tmp_path / "share"
    share.mkdir()
    card_scan = scanner.scan(str(card), idx)
    importer.ImportJob(items=card_scan.plan("new"), verify="hash", share_root=str(share),
                       index=idx, source={"label": "CARD"}, schema="{label}",
                       report=lambda p: None, decide=lambda e: "cancel",
                       ensure_share=lambda: None, cancel=threading.Event()).run()
    # now the camera by cable: the 2 files from the card are known
    items = camera_items(cam)
    cam_scan = scanner.scan_items(items, idx, brand="Canon")
    assert cam_scan.known_files == 2 and cam_scan.new_files == 2
    res = importer.ImportJob(items=cam_scan.plan("new"), verify="full", share_root=str(share),
                             index=idx, source={"model": "Canon EOS R6", "brand": "Canon"},
                             schema="{label}", report=lambda p: None,
                             decide=lambda e: "cancel", ensure_share=lambda: None,
                             cancel=threading.Event()).run()
    assert res.status == "done" and res.files == 2 and res.folder == "Canon"
    for path, data in list(files.items())[2:]:
        rel = path.split("/", 2)[2]
        assert (share / "Canon" / rel).read_bytes() == data
