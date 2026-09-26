"""cpio "newc" archive reader/writer, as used by the Linux initramfs."""

from __future__ import annotations

import gzip
import io
import stat
from dataclasses import dataclass

MAGIC = b"070701"
TRAILER = "TRAILER!!!"


@dataclass
class Entry:
    name: str
    mode: int
    data: bytes = b""
    mtime: int = 0
    uid: int = 0
    gid: int = 0


def _pad4(n: int) -> int:
    return (4 - n % 4) % 4


def read_entries(data: bytes):
    """Yield Entry objects from one or more concatenated, uncompressed
    newc archives."""
    i = 0
    while i < len(data):
        if data[i:i + 6] != MAGIC:
            j = data.find(MAGIC, i)
            if j < 0:
                return
            i = j
        h = data[i:i + 110]
        f = [int(h[6 + 8 * k:14 + 8 * k], 16) for k in range(13)]
        mode, uid, gid, mtime, fsize, nsize = f[1], f[2], f[3], f[5], f[6], f[11]
        name = data[i + 110:i + 110 + nsize - 1].decode("utf-8", errors="replace")
        off = i + 110 + nsize
        off += _pad4(off)
        body = data[off:off + fsize]
        i = off + fsize + _pad4(fsize)
        if name == TRAILER:
            continue
        yield Entry(name=name, mode=mode, data=body, mtime=mtime, uid=uid, gid=gid)


def read_gzip_initramfs(path: str) -> list[Entry]:
    with gzip.open(path, "rb") as f:
        return list(read_entries(f.read()))


class Writer:
    """Build a newc archive. Parent directories are added automatically."""

    def __init__(self, mtime: int = 0):
        self.buf = io.BytesIO()
        self.ino = 1000
        self.mtime = mtime
        self.dirs: set[str] = set()

    def _header(self, name: str, mode: int, size: int, nlink: int = 1) -> None:
        nb = name.encode() + b"\0"
        fields = [self.ino, mode, 0, 0, nlink, self.mtime, size, 0, 0, 0, 0, len(nb), 0]
        self.ino += 1
        self.buf.write(MAGIC + b"".join(b"%08X" % v for v in fields))
        self.buf.write(nb)
        self.buf.write(b"\0" * _pad4(110 + len(nb)))

    def _parents(self, name: str) -> None:
        parts = name.split("/")[:-1]
        for k in range(1, len(parts) + 1):
            d = "/".join(parts[:k])
            if d and d not in self.dirs:
                self.dirs.add(d)
                self._header(d, stat.S_IFDIR | 0o755, 0, 2)

    def directory(self, name: str, perm: int = 0o755) -> None:
        name = name.strip("/")
        self._parents(name)
        if name not in self.dirs:
            self.dirs.add(name)
            self._header(name, stat.S_IFDIR | perm, 0, 2)

    def file(self, name: str, data: bytes, perm: int = 0o644) -> None:
        name = name.strip("/")
        self._parents(name)
        self._header(name, stat.S_IFREG | perm, len(data))
        self.buf.write(data)
        self.buf.write(b"\0" * _pad4(len(data)))

    def symlink(self, name: str, target: str) -> None:
        name = name.strip("/")
        self._parents(name)
        t = target.encode()
        self._header(name, stat.S_IFLNK | 0o777, len(t))
        self.buf.write(t)
        self.buf.write(b"\0" * _pad4(len(t)))

    def finish(self) -> bytes:
        self._header(TRAILER, 0, 0)
        return self.buf.getvalue()

    def finish_gzip(self) -> bytes:
        return gzip.compress(self.finish(), compresslevel=9, mtime=0)
