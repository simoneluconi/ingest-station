"""Tolerant access to kernel-provided text (sysfs, /proc, tool output).

Device strings are not guaranteed to be UTF-8: the Wyse 3040 eMMC reports a
model name like b"MMC H8G4a\\x92". Everything read from the kernel or from
device tools goes through these helpers and is decoded with
errors="replace", never strictly.
"""

from __future__ import annotations

import os
import subprocess


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "rb") as f:
            return decode(f.read()).strip()
    except OSError:
        return default


def read_int(path: str, default: int = 0) -> int:
    try:
        return int(read_text(path, ""))
    except ValueError:
        return default


def run(cmd: list[str], check: bool = True, timeout: float = 120) -> str:
    """Run a command and return its output decoded tolerantly."""
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         timeout=timeout)
    out = decode(res.stdout)
    if check and res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd, out)
    return out


def kernel_cmdline(path: str = "/proc/cmdline") -> dict[str, str]:
    """Parse the kernel command line into a dict (flags map to "")."""
    opts: dict[str, str] = {}
    for tok in read_text(path).split():
        key, _, value = tok.partition("=")
        opts[key] = value
    return opts


def sync() -> None:
    """os.sync() where available (not on Windows, used in simulation)."""
    if hasattr(os, "sync"):
        os.sync()


def listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []
