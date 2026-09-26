"""File fingerprints used to recognise files that were already imported.

A file is identified by its size plus the xxh128 of its first and last MiB
(not by name: cameras reuse names). Files without random access (PTP/MTP
objects) fall back to name + size + date.
"""

from __future__ import annotations

import os

import xxhash

EDGE = 1 << 20  # 1 MiB


def mass_storage(size: int, head: bytes, tail: bytes) -> str:
    """Fingerprint from the first and last MiB (for files up to 2 MiB the
    head and tail overlap; the whole content is covered)."""
    h = xxhash.xxh3_128()
    h.update(head)
    h.update(tail)
    return f"{size}:{h.hexdigest()}"


def of_file(path: str, size: int | None = None) -> str:
    """Read the edges of a file and fingerprint it."""
    with open(path, "rb") as f:
        if size is None:
            size = os.fstat(f.fileno()).st_size
        head = f.read(EDGE)
        if size > EDGE:
            f.seek(max(size - EDGE, EDGE) if size < 2 * EDGE else size - EDGE)
            tail = f.read(EDGE)
        else:
            tail = b""
    return mass_storage(size, head, tail)


def ptp(name: str, size: int, mtime: int) -> str:
    return f"ptp:{size}:{int(mtime)}:{name.upper()}"


def size_of(fp: str) -> int | None:
    """Size encoded in a mass-storage fingerprint."""
    head = fp.split(":", 1)[0]
    return int(head) if head.isdigit() else None


class EdgeCollector:
    """Builds the fingerprint while a file is streamed (copy engine): keeps
    the first MiB and a rolling last MiB without a second read."""

    def __init__(self, size: int):
        self.size = size
        self.head = bytearray()
        self.tail = bytearray()
        self.pos = 0

    def update(self, chunk) -> None:
        mv = memoryview(chunk)
        if len(self.head) < EDGE:
            take = min(EDGE - len(self.head), len(mv))
            self.head += mv[:take]
        start = self.pos
        end = start + len(mv)
        self.pos = end
        if self.size > EDGE:
            # tail = bytes [tail_start, size) where tail_start >= EDGE
            tail_start = max(self.size - EDGE, EDGE) if self.size < 2 * EDGE else self.size - EDGE
            if end > tail_start:
                self.tail += mv[max(0, tail_start - start):]

    def fingerprint(self) -> str:
        return mass_storage(self.size, bytes(self.head), bytes(self.tail))
