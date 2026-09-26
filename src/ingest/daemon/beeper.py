"""Status beeps with distinct patterns (start, success, error).

Backends: "none" (default: the unit may have no speaker) and "pcspkr" (the
PC speaker through its evdev device). Audio devices (DisplayPort, USB)
arrive with the GUI phase.
"""

from __future__ import annotations

import glob
import os
import struct
import threading
import time

EV_SND, SND_TONE = 0x12, 0x02
# (frequency Hz, duration s, pause s)
PATTERNS = {
    "start": [(880, 0.08, 0.0)],
    "ok": [(988, 0.12, 0.06), (1319, 0.20, 0.0)],
    "error": [(330, 0.35, 0.12), (330, 0.35, 0.12), (262, 0.6, 0.0)],
}


def _pcspkr_device() -> str | None:
    for path in glob.glob("/dev/input/by-path/*pcspkr*"):
        return path
    for name_file in glob.glob("/sys/class/input/event*/device/name"):
        try:
            with open(name_file, encoding="utf-8", errors="replace") as f:
                if "speaker" in f.read().lower():
                    return "/dev/input/" + name_file.split("/")[4]
        except OSError:
            continue
    return None


def _event(freq: int) -> bytes:
    # struct input_event on 64-bit: timeval (2 x long), u16 type, u16 code, s32 value
    return struct.pack("llHHi", 0, 0, EV_SND, SND_TONE, freq)


class Beeper:
    def __init__(self, backend: str = "none"):
        self.backend = backend
        self._lock = threading.Lock()

    def available(self) -> dict:
        return {"pcspkr": _pcspkr_device() is not None}

    def play(self, pattern: str) -> None:
        if self.backend == "none" or pattern not in PATTERNS:
            return
        threading.Thread(target=self._play, args=(pattern,), daemon=True).start()

    def _play(self, pattern: str) -> None:
        with self._lock:
            if self.backend == "pcspkr":
                dev = _pcspkr_device()
                if not dev:
                    return
                try:
                    fd = os.open(dev, os.O_WRONLY)
                except OSError:
                    return
                try:
                    for freq, dur, pause in PATTERNS[pattern]:
                        os.write(fd, _event(freq))
                        time.sleep(dur)
                        os.write(fd, _event(0))
                        time.sleep(pause)
                finally:
                    os.write(fd, _event(0))
                    os.close(fd)
