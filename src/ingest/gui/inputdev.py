"""Input from the kernel event devices (pure Python): keyboards and numeric
keypads, mice and touchpads, touchscreens, the power button.

Devices are grabbed (EVIOCGRAB) so keystrokes do not also reach the text
console behind the GUI. New devices are picked up every few seconds.
"""

from __future__ import annotations

import errno
import glob
import os
import struct
import time
from dataclasses import dataclass, field

from ingest.common import sysfs

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
REL_X, REL_Y, REL_WHEEL = 0x00, 0x01, 0x08
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A
KEY_POWER = 116
EVENT = struct.Struct("llHHi")      # struct input_event on 64-bit
EVIOCGRAB = (1 << 30) | (4 << 16) | (ord("E") << 8) | 0x90


def EVIOCGABS(code: int) -> int:
    return (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + code)


# key codes
KEY = dict(ESC=1, BACKSPACE=14, TAB=15, ENTER=28, LEFTCTRL=29, LEFTSHIFT=42, RIGHTSHIFT=54,
           LEFTALT=56, SPACE=57, KPENTER=96, RIGHTCTRL=97, RIGHTALT=100, HOME=102, UP=103,
           PAGEUP=104, LEFT=105, RIGHT=106, END=107, DOWN=108, PAGEDOWN=109, DELETE=111,
           KP7=71, KP8=72, KP9=73, KP4=75, KP5=76, KP6=77, KP1=79, KP2=80, KP3=81, KP0=82,
           KPDOT=83, KPMINUS=74, KPPLUS=78, KPSLASH=98, KPASTERISK=55, BACK=158, F1=59,
           MENU=139, COMPOSE=127)

# navigation actions from keys (numeric keypads included, NumLock ignored)
NAV = {KEY["UP"]: "up", KEY["DOWN"]: "down", KEY["LEFT"]: "left", KEY["RIGHT"]: "right",
       KEY["ENTER"]: "ok", KEY["KPENTER"]: "ok", KEY["ESC"]: "back", KEY["BACK"]: "back",
       KEY["TAB"]: "next", KEY["HOME"]: "home", KEY["PAGEUP"]: "pageup",
       KEY["PAGEDOWN"]: "pagedown", KEY["F1"]: "settings", KEY["MENU"]: "settings",
       KEY["COMPOSE"]: "settings"}
KEYPAD_NAV = {KEY["KP8"]: "up", KEY["KP2"]: "down", KEY["KP4"]: "left", KEY["KP6"]: "right",
              KEY["KP5"]: "ok", KEY["KPPLUS"]: "next", KEY["KPMINUS"]: "back"}

_ROWS = {  # first keycode of each row
    2: 2, 16: 16, 30: 30, 44: 44}
LAYOUTS = {
    "us": {"normal": {2: "1234567890-=", 16: "qwertyuiop[]", 30: "asdfghjkl;'`", 44: "zxcvbnm,./",
                      43: "\\", 86: "\\"},
           "shift": {2: '!@#$%^&*()_+', 16: "QWERTYUIOP{}", 30: 'ASDFGHJKL:"~', 44: "ZXCVBNM<>?",
                     43: "|", 86: "|"},
           "altgr": {}},
    "it": {"normal": {2: "1234567890'ì", 16: "qwertyuiopè+", 30: "asdfghjklòà\\", 44: "zxcvbnm,.-",
                      43: "ù", 86: "<"},
           "shift": {2: '!"£$%&/()=?^', 16: "QWERTYUIOPé*", 30: "ASDFGHJKLç°|", 44: "ZXCVBNM;:_",
                     43: "§", 86: ">"},
           "altgr": {26: "[", 27: "]", 39: "@", 40: "#", 18: "€", 43: "`", 12: "`", 13: "~"}},
}
KEYPAD_CHARS = {KEY["KP0"]: "0", KEY["KP1"]: "1", KEY["KP2"]: "2", KEY["KP3"]: "3",
                KEY["KP4"]: "4", KEY["KP5"]: "5", KEY["KP6"]: "6", KEY["KP7"]: "7",
                KEY["KP8"]: "8", KEY["KP9"]: "9", KEY["KPDOT"]: ".", KEY["KPSLASH"]: "/",
                KEY["KPASTERISK"]: "*", KEY["KPMINUS"]: "-", KEY["KPPLUS"]: "+"}


def char_for(code: int, shift: bool, altgr: bool, layout: str = "it") -> str | None:
    lay = LAYOUTS.get(layout, LAYOUTS["us"])
    if code == KEY["SPACE"]:
        return " "
    if code in KEYPAD_CHARS:
        return KEYPAD_CHARS[code]
    if altgr:
        return lay["altgr"].get(code)
    table = lay["shift" if shift else "normal"]
    if code in table and len(table[code]) == 1:
        return table[code]
    for start in (2, 16, 30, 44):
        row = table.get(start)
        if row and start <= code < start + len(row):
            return row[code - start]
    return None


@dataclass
class Event:
    kind: str                  # key | nav | char | move | click | scroll | power
    action: str = ""           # nav action
    code: int = 0
    char: str = ""
    x: int = 0
    y: int = 0
    dy: int = 0
    pointer: str = ""          # mouse | touch


def _caps(evdir: str, name: str) -> int:
    words = sysfs.read_text(f"{evdir}/device/capabilities/{name}").split()
    value = 0
    for w in words:
        value = (value << 64) | int(w, 16)
    return value


@dataclass
class Device:
    path: str
    fd: int
    keyboard: bool = False
    mouse: bool = False
    touch: bool = False
    power: bool = False
    absx: tuple = (0, 1)
    absy: tuple = (0, 1)
    mt: bool = False
    buf: bytes = b""
    tx: int = 0
    ty: int = 0
    down: bool = False
    down_at: tuple = (0, 0)
    moved: bool = False
    pending_touch: dict = field(default_factory=dict)


class Inputs:
    def __init__(self, width: int, height: int, layout: str = "it"):
        self.width, self.height = width, height
        self.layout = layout
        self.devices: dict[str, Device] = {}
        self.pointer = (width // 2, height // 2)
        self.pointer_visible = False
        self.last_pointer_move = 0.0
        self.shift = self.ctrl = self.altgr = False
        self._last_scan = 0.0

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    # -- devices -----------------------------------------------------------
    def scan(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_scan < 3:
            return
        self._last_scan = time.monotonic()
        for evdir in sorted(glob.glob("/sys/class/input/event*")):
            path = "/dev/input/" + os.path.basename(evdir)
            if path in self.devices:
                continue
            key, rel, absc = _caps(evdir, "key"), _caps(evdir, "rel"), _caps(evdir, "abs")
            keyboard = bool(key >> 30 & 1) and bool(key >> 28 & 1)     # KEY_A and KEY_ENTER
            keypad = bool(key >> KEY["KP5"] & 1) and bool(key >> KEY["KPENTER"] & 1)
            mouse = bool(rel >> REL_X & 1) and bool(rel >> REL_Y & 1) and bool(key >> BTN_LEFT & 1)
            touch = (bool(absc >> ABS_MT_POSITION_X & 1) or bool(absc >> ABS_X & 1)) and \
                bool(key >> BTN_TOUCH & 1) and not mouse
            power = bool(key >> KEY_POWER & 1) and not keyboard
            if not (keyboard or keypad or mouse or touch or power):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                continue
            dev = Device(path, fd, keyboard=keyboard or keypad, mouse=mouse, touch=touch,
                         power=power, mt=bool(absc >> ABS_MT_POSITION_X & 1))
            if touch:
                dev.absx = self._absinfo(fd, ABS_MT_POSITION_X if dev.mt else ABS_X)
                dev.absy = self._absinfo(fd, ABS_MT_POSITION_Y if dev.mt else ABS_Y)
            try:
                import fcntl
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError:
                pass
            self.devices[path] = dev

    @staticmethod
    def _absinfo(fd: int, code: int) -> tuple:
        import fcntl
        buf = bytearray(24)
        try:
            fcntl.ioctl(fd, EVIOCGABS(code), buf, True)
            _val, lo, hi = struct.unpack_from("iii", buf)
            return (lo, hi if hi > lo else lo + 1)
        except OSError:
            return (0, 1)

    def fds(self) -> list[int]:
        return [d.fd for d in self.devices.values()]

    def close(self) -> None:
        for d in self.devices.values():
            os.close(d.fd)
        self.devices.clear()

    # -- events ------------------------------------------------------------
    def read(self, fd: int) -> list[Event]:
        dev = next((d for d in self.devices.values() if d.fd == fd), None)
        if dev is None:
            return []
        try:
            data = os.read(fd, EVENT.size * 64)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EIO):
                os.close(fd)
                self.devices.pop(dev.path, None)
            return []
        dev.buf += data
        out: list[Event] = []
        n = len(dev.buf) // EVENT.size
        for i in range(n):
            _s, _us, typ, code, value = EVENT.unpack_from(dev.buf, i * EVENT.size)
            out.extend(self._event(dev, typ, code, value))
        dev.buf = dev.buf[n * EVENT.size:]
        return out

    def _event(self, dev: Device, typ: int, code: int, value: int) -> list[Event]:
        if typ == EV_KEY:
            return self._key(dev, code, value)
        if typ == EV_REL and dev.mouse:
            if code in (REL_X, REL_Y):
                x, y = self.pointer
                if code == REL_X:
                    x = max(0, min(self.width - 1, x + value * 2))
                else:
                    y = max(0, min(self.height - 1, y + value * 2))
                self.pointer = (x, y)
                self.pointer_visible = True
                self.last_pointer_move = time.monotonic()
                return [Event("move", x=x, y=y, pointer="mouse")]
            if code == REL_WHEEL:
                return [Event("scroll", dy=-value)]
        if typ == EV_ABS and dev.touch:
            lo, hi = dev.absx if code in (ABS_X, ABS_MT_POSITION_X) else dev.absy
            if code in (ABS_X, ABS_MT_POSITION_X):
                dev.tx = int((value - lo) * (self.width - 1) / (hi - lo))
            elif code in (ABS_Y, ABS_MT_POSITION_Y):
                dev.ty = int((value - lo) * (self.height - 1) / (hi - lo))
            elif code == ABS_MT_TRACKING_ID and value == -1 and dev.down:
                return self._touch_up(dev)
            if dev.down and (abs(dev.tx - dev.down_at[0]) > 40 or abs(dev.ty - dev.down_at[1]) > 40):
                dev.moved = True
        if typ == EV_SYN and dev.touch and dev.down and not dev.moved:
            return [Event("move", x=dev.tx, y=dev.ty, pointer="touch")]
        return []

    def _touch_up(self, dev: Device) -> list[Event]:
        dev.down = False
        self.pointer_visible = False
        if dev.moved:
            dy = dev.down_at[1] - dev.ty
            return [Event("scroll", dy=int(dy / 120))] if abs(dy) > 60 else []
        return [Event("click", x=dev.down_at[0], y=dev.down_at[1], pointer="touch")]

    def _key(self, dev: Device, code: int, value: int) -> list[Event]:
        if code in (KEY["LEFTSHIFT"], KEY["RIGHTSHIFT"]):
            self.shift = value != 0
            return []
        if code in (KEY["LEFTCTRL"], KEY["RIGHTCTRL"]):
            self.ctrl = value != 0
            return []
        if code == KEY["RIGHTALT"]:
            self.altgr = value != 0
            return []
        if code == BTN_LEFT and dev.mouse:
            if value == 1:
                return [Event("click", x=self.pointer[0], y=self.pointer[1], pointer="mouse")]
            return []
        if code == BTN_TOUCH and dev.touch:
            if value == 1:
                dev.down, dev.moved = True, False
                dev.down_at = (dev.tx, dev.ty)
                return []
            if value == 0 and dev.down:
                return self._touch_up(dev)
            return []
        if value == 0:          # releases are not needed
            return []
        if code == KEY_POWER:
            return [Event("power")] if value == 1 else []
        evs = [Event("key", code=code)]
        ch = None if self.ctrl else char_for(code, self.shift, self.altgr, self.layout)
        if ch:
            evs.append(Event("char", char=ch, code=code))
        action = NAV.get(code) or KEYPAD_NAV.get(code)
        if action:
            evs.append(Event("nav", action=action, code=code))
        if code == KEY["BACKSPACE"]:
            evs.append(Event("nav", action="backspace", code=code))
        return evs
