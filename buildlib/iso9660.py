"""Minimal ISO9660 reader with Rock Ridge names (pure Python)."""

from __future__ import annotations

import struct

SECTOR = 2048


class ISO9660:
    def __init__(self, path: str):
        self.f = open(path, "rb")
        pvd = self._read(16 * SECTOR, SECTOR)
        if pvd[1:6] != b"CD001" or pvd[0] != 1:
            raise ValueError(f"{path}: not an ISO9660 image")
        self.volume_id = pvd[40:72].decode("ascii", errors="replace").strip()
        self.files: dict[str, tuple[int, int]] = {}
        self._walk(pvd[156:190], "")

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read(self, pos: int, size: int) -> bytes:
        self.f.seek(pos)
        return self.f.read(size)

    @staticmethod
    def _rr_name(sua: bytes) -> str | None:
        name, i = b"", 0
        while i + 4 <= len(sua):
            sig, length = sua[i:i + 2], sua[i + 2]
            if length == 0:
                break
            if sig == b"NM":
                name += sua[i + 5:i + length]
            i += length
        return name.decode("utf-8", errors="replace") if name else None

    def _walk(self, rec: bytes, path: str) -> None:
        lba, size = struct.unpack("<I", rec[2:6])[0], struct.unpack("<I", rec[10:14])[0]
        data = self._read(lba * SECTOR, size)
        i = 0
        while i < len(data):
            length = data[i]
            if length == 0:
                i = (i // SECTOR + 1) * SECTOR
                continue
            r = data[i:i + length]
            nlen = r[32]
            raw_name = r[33:33 + nlen]
            if raw_name not in (b"\x00", b"\x01"):
                sua = r[33 + nlen + ((nlen + 1) % 2):]
                name = self._rr_name(sua) or raw_name.decode("ascii").split(";")[0].lower()
                full = f"{path}/{name}"
                if r[25] & 2:
                    self._walk(r, full)
                else:
                    self.files[full] = (struct.unpack("<I", r[2:6])[0] * SECTOR,
                                        struct.unpack("<I", r[10:14])[0])
            i += length

    def offset(self, path: str) -> tuple[int, int]:
        """(byte offset, size) of a file inside the image."""
        return self.files[path]

    def read(self, path: str) -> bytes:
        off, size = self.files[path]
        return self._read(off, size)

    def extract(self, path: str, dest: str, chunk: int = 8 << 20) -> None:
        off, size = self.files[path]
        self.f.seek(off)
        with open(dest, "wb") as out:
            while size:
                buf = self.f.read(min(chunk, size))
                out.write(buf)
                size -= len(buf)
