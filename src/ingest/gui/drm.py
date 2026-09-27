"""Screen output through KMS/DRM "dumb buffers", with pure-Python ioctls
(no Mesa/EGL). The GUI draws into an off-screen pygame surface whose pixel
format (XRGB8888) is the one of the dumb buffer, so presenting a frame is a
single memory copy. Falls back to the fbdev device when DRM is unavailable.
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import mmap
import os

from ingest.common import sysfs

u8, u16, u32, u64 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint64


def _IOWR(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (0x64 << 8) | nr


def _IO(nr: int) -> int:
    return (0x64 << 8) | nr


class ModeInfo(ctypes.Structure):
    _fields_ = [("clock", u32), ("hdisplay", u16), ("hsync_start", u16), ("hsync_end", u16),
                ("htotal", u16), ("hskew", u16), ("vdisplay", u16), ("vsync_start", u16),
                ("vsync_end", u16), ("vtotal", u16), ("vscan", u16), ("vrefresh", u32),
                ("flags", u32), ("type", u32), ("name", ctypes.c_char * 32)]


class CardRes(ctypes.Structure):
    _fields_ = [("fb_id_ptr", u64), ("crtc_id_ptr", u64), ("connector_id_ptr", u64),
                ("encoder_id_ptr", u64), ("count_fbs", u32), ("count_crtcs", u32),
                ("count_connectors", u32), ("count_encoders", u32), ("min_width", u32),
                ("max_width", u32), ("min_height", u32), ("max_height", u32)]


class GetConnector(ctypes.Structure):
    _fields_ = [("encoders_ptr", u64), ("modes_ptr", u64), ("props_ptr", u64),
                ("prop_values_ptr", u64), ("count_modes", u32), ("count_props", u32),
                ("count_encoders", u32), ("encoder_id", u32), ("connector_id", u32),
                ("connector_type", u32), ("connector_type_id", u32), ("connection", u32),
                ("mm_width", u32), ("mm_height", u32), ("subpixel", u32), ("pad", u32)]


class GetEncoder(ctypes.Structure):
    _fields_ = [("encoder_id", u32), ("encoder_type", u32), ("crtc_id", u32),
                ("possible_crtcs", u32), ("possible_clones", u32)]


class CreateDumb(ctypes.Structure):
    _fields_ = [("height", u32), ("width", u32), ("bpp", u32), ("flags", u32),
                ("handle", u32), ("pitch", u32), ("size", u64)]


class FbCmd(ctypes.Structure):
    _fields_ = [("fb_id", u32), ("width", u32), ("height", u32), ("pitch", u32),
                ("bpp", u32), ("depth", u32), ("handle", u32)]


class MapDumb(ctypes.Structure):
    _fields_ = [("handle", u32), ("pad", u32), ("offset", u64)]


class Crtc(ctypes.Structure):
    _fields_ = [("set_connectors_ptr", u64), ("count_connectors", u32), ("crtc_id", u32),
                ("fb_id", u32), ("x", u32), ("y", u32), ("gamma_size", u32),
                ("mode_valid", u32), ("mode", ModeInfo)]


DRM_IOCTL_SET_MASTER = _IO(0x1E)
DRM_IOCTL_DROP_MASTER = _IO(0x1F)
DRM_IOCTL_MODE_GETRESOURCES = _IOWR(0xA0, ctypes.sizeof(CardRes))
DRM_IOCTL_MODE_SETCRTC = _IOWR(0xA2, ctypes.sizeof(Crtc))
DRM_IOCTL_MODE_GETENCODER = _IOWR(0xA6, ctypes.sizeof(GetEncoder))
DRM_IOCTL_MODE_GETCONNECTOR = _IOWR(0xA7, ctypes.sizeof(GetConnector))
DRM_IOCTL_MODE_ADDFB = _IOWR(0xAE, ctypes.sizeof(FbCmd))
DRM_IOCTL_MODE_RMFB = _IOWR(0xAF, 4)
DRM_IOCTL_MODE_CREATE_DUMB = _IOWR(0xB2, ctypes.sizeof(CreateDumb))
DRM_IOCTL_MODE_MAP_DUMB = _IOWR(0xB3, ctypes.sizeof(MapDumb))
DRM_IOCTL_MODE_DESTROY_DUMB = _IOWR(0xB4, 4)
DRM_MODE_TYPE_PREFERRED = 1 << 3
MAX_W, MAX_H = 1920, 1200   # larger modes cost too much CPU per frame on the Atom


class DisplayError(Exception):
    pass


def _ioctl(fd: int, req: int, obj) -> None:
    fcntl.ioctl(fd, req, obj, True)


class DrmDisplay:
    """One CRTC driving the first connected connector."""

    def __init__(self):
        self.fd = -1
        self.card = ""
        self.width = self.height = 0
        self.pitch = 0
        self.map: mmap.mmap | None = None
        self.fb_id = 0
        self.handle = 0
        self.connector_id = 0
        self.connector_name = ""

    # -- discovery -------------------------------------------------------------
    @staticmethod
    def cards() -> list[str]:
        return sorted(glob.glob("/dev/dri/card*"))

    @staticmethod
    def connected_connector() -> str | None:
        """Name of a connected connector (sysfs, cheap to poll)."""
        for st in sorted(glob.glob("/sys/class/drm/card*-*/status")):
            if sysfs.read_text(st) == "connected":
                return os.path.basename(os.path.dirname(st))
        return None

    def open(self) -> None:
        name = self.connected_connector()
        if not name:
            raise DisplayError("no monitor connected")
        self.card = "/dev/dri/" + name.split("-", 1)[0]
        self.connector_name = name
        self.fd = os.open(self.card, os.O_RDWR | os.O_CLOEXEC)
        try:
            _ioctl(self.fd, DRM_IOCTL_SET_MASTER, ctypes.c_int(0))
        except OSError:
            pass   # not fatal when nobody else holds master
        self._setup()

    def _resources(self):
        res = CardRes()
        _ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        crtcs = (u32 * max(res.count_crtcs, 1))()
        conns = (u32 * max(res.count_connectors, 1))()
        encs = (u32 * max(res.count_encoders, 1))()
        fbs = (u32 * max(res.count_fbs, 1))()
        res.crtc_id_ptr = ctypes.addressof(crtcs)
        res.connector_id_ptr = ctypes.addressof(conns)
        res.encoder_id_ptr = ctypes.addressof(encs)
        res.fb_id_ptr = ctypes.addressof(fbs)
        _ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        return (list(crtcs[:res.count_crtcs]), list(conns[:res.count_connectors]),
                list(encs[:res.count_encoders]))

    def _connector(self, cid: int):
        c = GetConnector(connector_id=cid)
        _ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, c)
        modes = (ModeInfo * max(c.count_modes, 1))()
        encs = (u32 * max(c.count_encoders, 1))()
        props = (u32 * max(c.count_props, 1))()
        vals = (u64 * max(c.count_props, 1))()
        c.modes_ptr = ctypes.addressof(modes)
        c.encoders_ptr = ctypes.addressof(encs)
        c.props_ptr = ctypes.addressof(props)
        c.prop_values_ptr = ctypes.addressof(vals)
        _ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, c)
        return c, list(modes[:c.count_modes]), list(encs[:c.count_encoders])

    @staticmethod
    def pick_mode(modes: list[ModeInfo]) -> ModeInfo:
        usable = [m for m in modes if m.hdisplay <= MAX_W and m.vdisplay <= MAX_H]
        if not usable:
            raise DisplayError("no usable display mode")
        pref = [m for m in usable if m.type & DRM_MODE_TYPE_PREFERRED]
        return pref[0] if pref else max(usable, key=lambda m: (m.hdisplay * m.vdisplay,
                                                                m.vrefresh))

    def _setup(self) -> None:
        crtcs, conns, _encs = self._resources()
        for cid in conns:
            c, modes, encs = self._connector(cid)
            if c.connection == 1 and modes:
                break
        else:
            raise DisplayError("no connected connector with modes")
        mode = self.pick_mode(modes)
        crtc_id = 0
        for eid in ([c.encoder_id] if c.encoder_id else []) + encs:
            e = GetEncoder(encoder_id=eid)
            try:
                _ioctl(self.fd, DRM_IOCTL_MODE_GETENCODER, e)
            except OSError:
                continue
            if e.crtc_id:
                crtc_id = e.crtc_id
                break
            for i, cr in enumerate(crtcs):
                if e.possible_crtcs & (1 << i):
                    crtc_id = cr
                    break
            if crtc_id:
                break
        if not crtc_id:
            raise DisplayError("no CRTC for the connector")
        w, h = mode.hdisplay, mode.vdisplay
        dumb = CreateDumb(width=w, height=h, bpp=32)
        _ioctl(self.fd, DRM_IOCTL_MODE_CREATE_DUMB, dumb)
        fb = FbCmd(width=w, height=h, pitch=dumb.pitch, bpp=32, depth=24, handle=dumb.handle)
        _ioctl(self.fd, DRM_IOCTL_MODE_ADDFB, fb)
        mp = MapDumb(handle=dumb.handle)
        _ioctl(self.fd, DRM_IOCTL_MODE_MAP_DUMB, mp)
        self.map = mmap.mmap(self.fd, dumb.size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE, offset=mp.offset)
        conn = (u32 * 1)(cid)
        crtc = Crtc(set_connectors_ptr=ctypes.addressof(conn), count_connectors=1,
                    crtc_id=crtc_id, fb_id=fb.fb_id, mode_valid=1, mode=mode)
        _ioctl(self.fd, DRM_IOCTL_MODE_SETCRTC, crtc)
        self.width, self.height, self.pitch = w, h, dumb.pitch
        self.fb_id, self.handle, self.connector_id = fb.fb_id, dumb.handle, cid

    # -- frames ------------------------------------------------------------------
    def present(self, raw: bytes) -> None:
        """Copy an XRGB8888 frame (width*4 bytes per row) to the screen."""
        if self.map is None:
            return
        row = self.width * 4
        if self.pitch == row:
            self.map[:len(raw)] = raw
        else:
            for y in range(self.height):
                self.map[y * self.pitch:y * self.pitch + row] = raw[y * row:(y + 1) * row]

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            if self.map is not None:
                self.map.close()
            if self.fb_id:
                _ioctl(self.fd, DRM_IOCTL_MODE_RMFB, ctypes.c_uint32(self.fb_id))
            if self.handle:
                _ioctl(self.fd, DRM_IOCTL_MODE_DESTROY_DUMB, ctypes.c_uint32(self.handle))
            _ioctl(self.fd, DRM_IOCTL_DROP_MASTER, ctypes.c_int(0))
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd, self.map, self.fb_id, self.handle = -1, None, 0, 0


class FbDisplay:
    """Fallback: legacy framebuffer device (32 bpp only)."""

    def __init__(self, dev: str = "/dev/fb0"):
        self.dev = dev
        self.map = None
        self.width = self.height = self.pitch = 0
        self.connector_name = "fb0"

    def open(self) -> None:
        base = "/sys/class/graphics/" + os.path.basename(self.dev)
        w, h = (int(v) for v in sysfs.read_text(f"{base}/virtual_size", "0,0").split(","))
        if sysfs.read_int(f"{base}/bits_per_pixel") != 32 or not w:
            raise DisplayError("framebuffer is not 32 bpp")
        self.width, self.height = w, h
        self.pitch = sysfs.read_int(f"{base}/stride") or w * 4
        fd = os.open(self.dev, os.O_RDWR)
        self.map = mmap.mmap(fd, self.pitch * h, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        os.close(fd)

    present = DrmDisplay.present

    def close(self) -> None:
        if self.map is not None:
            self.map.close()
            self.map = None


def open_display():
    """DRM first, framebuffer as a fallback; raises DisplayError if neither."""
    errors = []
    for cls in (DrmDisplay, FbDisplay):
        d = cls()
        try:
            d.open()
            return d
        except (OSError, DisplayError) as e:
            errors.append(f"{cls.__name__}: {e}")
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
    raise DisplayError("; ".join(errors))
