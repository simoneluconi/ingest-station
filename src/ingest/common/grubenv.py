"""GRUB environment block (grubenv) read/write.

The file is exactly 1024 bytes: a header line, `key=value` lines and '#'
padding. GRUB's `save_env` rewrites it in place, so we must never change its
size or location on disk: updates overwrite the existing file in place and
fsync it.
"""

from __future__ import annotations

import os

SIZE = 1024
HEADER = b"# GRUB Environment Block\n"


class GrubEnvError(Exception):
    pass


def parse(data: bytes) -> dict[str, str]:
    if len(data) != SIZE or not data.startswith(HEADER):
        raise GrubEnvError("not a valid grubenv block")
    env: dict[str, str] = {}
    for line in data[len(HEADER):].split(b"\n"):
        if not line or line.startswith(b"#"):
            continue
        key, sep, value = line.partition(b"=")
        if sep:
            # GRUB escapes backslash and newline; we never store either
            env[key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")
    return env


def serialize(env: dict[str, str]) -> bytes:
    body = bytearray(HEADER)
    for key, value in env.items():
        if not key or "=" in key or "\n" in key or "\n" in value or "\\" in value:
            raise GrubEnvError(f"invalid grubenv entry {key!r}")
        body += f"{key}={value}\n".encode()
    if len(body) > SIZE:
        raise GrubEnvError("grubenv block overflow")
    return bytes(body) + b"#" * (SIZE - len(body))


def read(path: str) -> dict[str, str]:
    with open(path, "rb") as f:
        return parse(f.read())


def write(path: str, env: dict[str, str]) -> None:
    """Overwrite an existing grubenv in place (same size, same clusters)."""
    data = serialize(env)
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        if os.fstat(fd).st_size != SIZE:
            raise GrubEnvError(f"{path}: unexpected size, refusing to rewrite")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, data) != SIZE:
            raise GrubEnvError(f"{path}: short write")
        os.fsync(fd)
    finally:
        os.close(fd)


def create(path: str, env: dict[str, str]) -> None:
    """Create a new grubenv file (installer only)."""
    with open(path, "wb") as f:
        f.write(serialize(env))
        f.flush()
        os.fsync(f.fileno())
