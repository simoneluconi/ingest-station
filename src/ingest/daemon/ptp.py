"""Cameras and phones over PTP/MTP through libgphoto2 (ctypes, no FUSE).

Struct layouts match libgphoto2 2.5.33 (Alpine 3.24). libgphoto2 is not
thread-safe: every call on a camera holds that camera's lock, and only one
import runs at a time.

Files are read with partial reads (PTP GetPartialObject) in large chunks,
so the regular copy engine streams them like files on a card and computes
the same fingerprint: a file imported from the card reader is recognised
when the camera is connected by cable, and vice versa.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import threading
from dataclasses import dataclass

from ingest.common import sysfs

GP_OK = 0
GP_ERROR_NOT_SUPPORTED = -6
GP_FILE_TYPE_NORMAL = 1
GP_FILE_INFO_SIZE = 1 << 2
GP_FILE_INFO_MTIME = 1 << 7
USB_CLASS_STILL_IMAGE = 0x06


class PTPError(Exception):
    pass


class NotSupported(PTPError):
    pass


class CameraStorageInformation(ctypes.Structure):
    _fields_ = [("fields", ctypes.c_int), ("basedir", ctypes.c_char * 256),
                ("label", ctypes.c_char * 256), ("description", ctypes.c_char * 256),
                ("type", ctypes.c_int), ("fstype", ctypes.c_int), ("access", ctypes.c_int),
                ("capacitykbytes", ctypes.c_uint64), ("freekbytes", ctypes.c_uint64),
                ("freeimages", ctypes.c_uint64)]


class CameraFileInfoPreview(ctypes.Structure):
    _fields_ = [("fields", ctypes.c_int), ("status", ctypes.c_int), ("size", ctypes.c_uint64),
                ("type", ctypes.c_char * 64), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32)]


class CameraFileInfoFile(ctypes.Structure):
    _fields_ = [("fields", ctypes.c_int), ("status", ctypes.c_int), ("size", ctypes.c_uint64),
                ("type", ctypes.c_char * 64), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("permissions", ctypes.c_int),
                ("mtime", ctypes.c_int64)]   # time_t: 64 bit on musl


class CameraFileInfoAudio(ctypes.Structure):
    _fields_ = [("fields", ctypes.c_int), ("status", ctypes.c_int), ("size", ctypes.c_uint64),
                ("type", ctypes.c_char * 64)]


class CameraFileInfo(ctypes.Structure):
    _fields_ = [("preview", CameraFileInfoPreview), ("file", CameraFileInfoFile),
                ("audio", CameraFileInfoAudio)]


class CameraAbilities(ctypes.Structure):
    # passed by value to gp_camera_set_abilities: the size must be exact (2504)
    _fields_ = [("model", ctypes.c_char * 128), ("status", ctypes.c_int),
                ("port", ctypes.c_int), ("speed", ctypes.c_int * 64),
                ("operations", ctypes.c_int), ("file_operations", ctypes.c_int),
                ("folder_operations", ctypes.c_int), ("usb_vendor", ctypes.c_int),
                ("usb_product", ctypes.c_int), ("usb_class", ctypes.c_int),
                ("usb_subclass", ctypes.c_int), ("usb_protocol", ctypes.c_int),
                ("library", ctypes.c_char * 1024), ("id", ctypes.c_char * 1024),
                ("device_type", ctypes.c_int), ("reserved", ctypes.c_int * 7)]


_lib = None
_lib_lock = threading.Lock()


def lib():
    """Load libgphoto2 once (raises PTPError when it is missing)."""
    global _lib
    with _lib_lock:
        if _lib is None:
            name = ctypes.util.find_library("gphoto2") or "libgphoto2.so.6"
            try:
                g = ctypes.CDLL(name)
            except OSError as e:
                raise PTPError(f"libgphoto2 not available: {e}") from e
            vp, cp, ip = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
            g.gp_context_new.restype = vp
            g.gp_result_as_string.restype = cp
            g.gp_result_as_string.argtypes = [ip]
            for f, args in {
                "gp_list_new": [ctypes.POINTER(vp)], "gp_list_free": [vp],
                "gp_list_count": [vp], "gp_list_get_name": [vp, ip, ctypes.POINTER(cp)],
                "gp_list_get_value": [vp, ip, ctypes.POINTER(cp)],
                "gp_camera_new": [ctypes.POINTER(vp)], "gp_camera_init": [vp, vp],
                "gp_camera_exit": [vp, vp], "gp_camera_unref": [vp],
                "gp_camera_autodetect": [vp, vp],
                "gp_camera_set_abilities": [vp, CameraAbilities],
                "gp_camera_set_port_info": [vp, vp],
                "gp_camera_get_storageinfo": [vp, ctypes.POINTER(ctypes.POINTER(
                    CameraStorageInformation)), ctypes.POINTER(ip), vp],
                "gp_camera_folder_list_files": [vp, cp, vp, vp],
                "gp_camera_folder_list_folders": [vp, cp, vp, vp],
                "gp_camera_file_get_info": [vp, cp, cp, ctypes.POINTER(CameraFileInfo), vp],
                "gp_camera_file_read": [vp, cp, cp, ip, ctypes.c_uint64, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_uint64), vp],
                "gp_abilities_list_new": [ctypes.POINTER(vp)], "gp_abilities_list_free": [vp],
                "gp_abilities_list_load": [vp, vp], "gp_abilities_list_lookup_model": [vp, cp],
                "gp_abilities_list_get_abilities": [vp, ip, ctypes.POINTER(CameraAbilities)],
                "gp_port_info_list_new": [ctypes.POINTER(vp)], "gp_port_info_list_free": [vp],
                "gp_port_info_list_load": [vp], "gp_port_info_list_lookup_path": [vp, cp],
                "gp_port_info_list_get_info": [vp, ip, ctypes.POINTER(vp)],
            }.items():
                fn = getattr(g, f)
                fn.argtypes = args
                fn.restype = ip
            _lib = g
    return _lib


def _check(rc: int, what: str) -> int:
    if rc < GP_OK:
        msg = sysfs.decode(lib().gp_result_as_string(rc) or b"")
        if rc == GP_ERROR_NOT_SUPPORTED:
            raise NotSupported(f"{what}: {msg}")
        raise PTPError(f"{what}: {msg} ({rc})")
    return rc


class _List:
    def __init__(self):
        self.ptr = ctypes.c_void_p()
        _check(lib().gp_list_new(ctypes.byref(self.ptr)), "gp_list_new")

    def items(self) -> list[tuple[str, str]]:
        g, out = lib(), []
        for i in range(max(g.gp_list_count(self.ptr), 0)):
            name, value = ctypes.c_char_p(), ctypes.c_char_p()
            g.gp_list_get_name(self.ptr, i, ctypes.byref(name))
            g.gp_list_get_value(self.ptr, i, ctypes.byref(value))
            out.append((sysfs.decode(name.value or b""), sysfs.decode(value.value or b"")))
        return out

    def __del__(self):
        if _lib is not None and self.ptr:
            _lib.gp_list_free(self.ptr)


# -- detection ------------------------------------------------------------

@dataclass
class UsbCamera:
    """A USB device exposing a still-image (PTP) or MTP interface."""
    sysdir: str
    busnum: int
    devnum: int
    serial: str
    vendor: str
    product: str

    @property
    def port(self) -> str:
        return f"usb:{self.busnum:03d},{self.devnum:03d}"


def usb_cameras(sys_root: str = "/sys") -> list[UsbCamera]:
    """PTP/MTP devices from sysfs (no libgphoto2 call: cheap to poll)."""
    out = []
    for dev in sorted(glob.glob(f"{sys_root}/bus/usb/devices/[0-9]*-*")):
        if ":" in os.path.basename(dev):
            continue   # interface, not device
        is_cam = False
        for itf in glob.glob(f"{dev}/{os.path.basename(dev)}:*"):
            cls = sysfs.read_text(f"{itf}/bInterfaceClass")
            name = sysfs.read_text(f"{itf}/interface").upper()
            if cls == "06" or "MTP" in name:
                is_cam = True
        if is_cam:
            out.append(UsbCamera(
                sysdir=dev, busnum=sysfs.read_int(f"{dev}/busnum"),
                devnum=sysfs.read_int(f"{dev}/devnum"), serial=sysfs.read_text(f"{dev}/serial"),
                vendor=sysfs.read_text(f"{dev}/manufacturer"),
                product=sysfs.read_text(f"{dev}/product")))
    return out


def autodetect() -> dict[str, str]:
    """port -> model, as seen by libgphoto2."""
    g = lib()
    ctx = g.gp_context_new()
    lst = _List()
    _check(g.gp_camera_autodetect(lst.ptr, ctx), "autodetect")
    return {port: model for model, port in lst.items()}


# -- camera ---------------------------------------------------------------

@dataclass
class Storage:
    basedir: str
    label: str
    description: str
    capacity: int
    free: int


@dataclass
class CamFile:
    folder: str
    name: str
    size: int
    mtime: float


class Camera:
    def __init__(self, model: str, port: str):
        self.model = model
        self.port = port
        self.lock = threading.RLock()
        g = lib()
        self.ctx = g.gp_context_new()
        self.cam = ctypes.c_void_p()
        with self.lock:
            _check(g.gp_camera_new(ctypes.byref(self.cam)), "gp_camera_new")
            self._select(model, port)
            _check(g.gp_camera_init(self.cam, self.ctx), f"cannot open {model}")

    def _select(self, model: str, port: str) -> None:
        g = lib()
        al = ctypes.c_void_p()
        _check(g.gp_abilities_list_new(ctypes.byref(al)), "abilities")
        try:
            _check(g.gp_abilities_list_load(al, self.ctx), "abilities load")
            idx = _check(g.gp_abilities_list_lookup_model(al, model.encode()), "unknown model")
            ab = CameraAbilities()
            _check(g.gp_abilities_list_get_abilities(al, idx, ctypes.byref(ab)), "abilities")
            _check(g.gp_camera_set_abilities(self.cam, ab), "set abilities")
        finally:
            g.gp_abilities_list_free(al)
        pl = ctypes.c_void_p()
        _check(g.gp_port_info_list_new(ctypes.byref(pl)), "ports")
        try:
            _check(g.gp_port_info_list_load(pl), "ports load")
            pidx = _check(g.gp_port_info_list_lookup_path(pl, port.encode()), "unknown port")
            info = ctypes.c_void_p()
            _check(g.gp_port_info_list_get_info(pl, pidx, ctypes.byref(info)), "port info")
            _check(g.gp_camera_set_port_info(self.cam, info), "set port")
        finally:
            g.gp_port_info_list_free(pl)

    def close(self) -> None:
        with self.lock:
            if self.cam:
                lib().gp_camera_exit(self.cam, self.ctx)
                lib().gp_camera_unref(self.cam)
                self.cam = ctypes.c_void_p()

    def storages(self) -> list[Storage]:
        g = lib()
        with self.lock:
            arr = ctypes.POINTER(CameraStorageInformation)()
            n = ctypes.c_int()
            try:
                _check(g.gp_camera_get_storageinfo(self.cam, ctypes.byref(arr), ctypes.byref(n),
                                                   self.ctx), "storage info")
            except NotSupported:
                return [Storage("/", "", "", 0, 0)]
            out = []
            for i in range(n.value):
                s = arr[i]
                out.append(Storage(sysfs.decode(s.basedir), sysfs.decode(s.label),
                                   sysfs.decode(s.description), s.capacitykbytes * 1024,
                                   s.freekbytes * 1024))
            return out

    def _list(self, fn, folder: str) -> list[str]:
        lst = _List()
        _check(fn(self.cam, folder.encode(), lst.ptr, self.ctx), f"list {folder}")
        return [name for name, _ in lst.items()]

    def walk(self, basedir: str, cancel: threading.Event | None = None) -> list[CamFile]:
        g = lib()
        out: list[CamFile] = []
        with self.lock:
            stack = [basedir.rstrip("/") or "/"]
            while stack:
                if cancel is not None and cancel.is_set():
                    break
                folder = stack.pop()
                for sub in sorted(self._list(g.gp_camera_folder_list_folders, folder),
                                  reverse=True):
                    stack.append(f"{folder.rstrip('/')}/{sub}")
                for name in sorted(self._list(g.gp_camera_folder_list_files, folder)):
                    info = CameraFileInfo()
                    _check(g.gp_camera_file_get_info(self.cam, folder.encode(), name.encode(),
                                                     ctypes.byref(info), self.ctx), name)
                    f = info.file
                    out.append(CamFile(folder, name,
                                       f.size if f.fields & GP_FILE_INFO_SIZE else 0,
                                       float(f.mtime) if f.fields & GP_FILE_INFO_MTIME else 0.0))
        return out

    def read(self, folder: str, name: str, offset: int, buf) -> int:
        """Partial read straight into `buf` (writable bytearray/memoryview)."""
        g = lib()
        mv = memoryview(buf).cast("B")
        size = ctypes.c_uint64(len(mv))
        cbuf = (ctypes.c_char * len(mv)).from_buffer(mv)
        with self.lock:
            _check(g.gp_camera_file_read(self.cam, folder.encode(), name.encode(),
                                         GP_FILE_TYPE_NORMAL, offset, cbuf,
                                         ctypes.byref(size), self.ctx), f"read {name}")
        return size.value


class CameraStream:
    """File-like object over a camera file, for the copy engine."""

    def __init__(self, camera: Camera, cfile: CamFile):
        self.camera = camera
        self.cfile = cfile
        self.pos = 0

    def readinto(self, buf) -> int:
        if self.pos >= self.cfile.size:
            return 0
        want = min(len(buf), self.cfile.size - self.pos)
        try:
            n = self.camera.read(self.cfile.folder, self.cfile.name, self.pos,
                                 memoryview(buf)[:want])
        except PTPError as e:
            raise OSError(5, str(e)) from e
        self.pos += n
        return n

    def read(self, n: int) -> bytes:
        buf = bytearray(min(n, max(self.cfile.size - self.pos, 0)))
        got = self.readinto(buf) if buf else 0
        return bytes(buf[:got])

    def seek(self, pos: int) -> None:
        self.pos = pos

    def fileno(self):
        raise OSError("camera stream has no file descriptor")

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
