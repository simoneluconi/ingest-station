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


def test_walk_reads_only_media_folders(monkeypatch):
    """A phone: photos in DCIM and Pictures; apps, caches and music elsewhere;
    Android/data cannot be listed. Only the photos are read, nothing fails."""
    folders = {
        "/store_1": ["Android", "DCIM", "Download", "Music", "Pictures"],
        "/store_1/DCIM": ["Camera", ".thumbnails"],
        "/store_1/DCIM/Camera": [],
        "/store_1/DCIM/.thumbnails": [],
        "/store_1/Pictures": ["Screenshots", "Locked"],
        "/store_1/Pictures/Screenshots": [],
        "/store_1/Pictures/Locked": None,          # cannot be listed
        "/store_1/Android": ["data"],
        "/store_1/Android/data": None,
    }
    files = {
        "/store_1/DCIM/Camera": ["IMG_1.jpg", "VID_2.mp4", ".pending-IMG_3.jpg"],
        "/store_1/DCIM/.thumbnails": ["t1.jpg"],
        "/store_1/Pictures/Screenshots": ["shot.png", "broken.png"],
        "/store_1/Music": ["song.mp3"],
        "/store_1/Download": ["file.pdf"],
    }
    listed = []

    def fake_list(self, fn, folder):
        listed.append(folder)
        table = folders if fn == "folders" else files
        if table.get(folder, []) is None:
            raise ptp.PTPError(f"list {folder}: Unspecified error (-1)")
        return list(table.get(folder, []))

    class FakeLib:
        gp_camera_folder_list_folders = "folders"
        gp_camera_folder_list_files = "files"

        @staticmethod
        def gp_camera_file_get_info(cam, folder, name, info_ref, ctx):
            if name == b"broken.png":
                return -1
            info = info_ref._obj
            info.file.fields = ptp.GP_FILE_INFO_SIZE | ptp.GP_FILE_INFO_MTIME
            info.file.size, info.file.mtime = 1000, 1_700_000_000
            return ptp.GP_OK
    monkeypatch.setattr(ptp, "lib", lambda: FakeLib)
    monkeypatch.setattr(ptp.Camera, "_list", fake_list)
    cam = ptp.Camera.__new__(ptp.Camera)
    cam.lock, cam.cam, cam.ctx = threading.RLock(), None, None
    got = sorted(f"{f.folder}/{f.name}" for f in cam.walk("/store_1"))
    assert got == ["/store_1/DCIM/Camera/IMG_1.jpg", "/store_1/DCIM/Camera/VID_2.mp4",
                   "/store_1/Pictures/Screenshots/shot.png"]
    # apps, music and downloads are never listed
    assert not any(f.startswith(("/store_1/Android", "/store_1/Music", "/store_1/Download"))
                   for f in listed)
    assert ptp.is_media_root("DCIM") and ptp.is_media_root("pictures")
    assert not ptp.is_media_root("Android")


def test_device_name():
    phone = ("Manufacturer: Samsung Electronics Co., Ltd.\nModel: SM-S928B\n"
             "  Version: S928BXXS4AXK1\n  Serial Number: R5CY50W3LTW\n")
    # cameras keep libgphoto2's name; phones get the reported manufacturer and model
    assert ptp.device_name("Canon EOS R6", phone) == "Canon EOS R6"
    assert ptp.device_name("Samsung Galaxy models (MTP)", phone) == "Samsung SM-S928B"
    assert ptp.device_name("Google Pixel (MTP)", "Manufacturer: Google\nModel: Pixel 8\n") == \
        "Google Pixel 8"
    # a model that already starts with the maker, or no summary at all
    assert ptp.device_name("Generic MTP device", "Manufacturer: OnePlus\nModel: OnePlus 12\n") == \
        "OnePlus 12"
    assert ptp.device_name("Samsung Galaxy models (MTP)", "") == "Samsung Galaxy"


def test_is_phone():
    assert ptp.is_phone("Samsung Galaxy models (MTP)") and ptp.is_phone("Generic MTP device")
    assert not ptp.is_phone("Canon EOS R6") and not ptp.is_phone("Nikon DSC Z 6II")
