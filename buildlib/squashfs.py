"""Minimal read-only SquashFS 4.0 reader (pure Python).

Used to pull kernel modules out of Alpine's modloop image on the build host,
where no squashfs tools are available. Supports gzip, xz and zstd compression,
basic/extended directories, files and symlinks.
"""

from __future__ import annotations

import lzma
import stat
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = 0x73717368
NO_FRAGMENT = 0xFFFFFFFF

T_DIR, T_FILE, T_SYMLINK = 1, 2, 3
T_LDIR, T_LFILE, T_LSYMLINK = 8, 9, 10


def _decompressor(comp_id: int):
    if comp_id == 1:
        return zlib.decompress
    if comp_id == 4:
        return lambda data: lzma.decompress(data, format=lzma.FORMAT_XZ)
    if comp_id == 6:
        from compression import zstd  # Python >= 3.14

        return zstd.decompress
    raise ValueError(f"unsupported squashfs compressor id {comp_id}")


@dataclass
class Inode:
    type: int
    mode: int
    mtime: int
    number: int
    size: int = 0
    blocks_start: int = 0
    block_sizes: list[int] = field(default_factory=list)
    fragment: int = NO_FRAGMENT
    frag_offset: int = 0
    dir_block: int = 0
    dir_offset: int = 0
    target: str = ""

    @property
    def is_dir(self) -> bool:
        return self.type in (T_DIR, T_LDIR)

    @property
    def is_file(self) -> bool:
        return self.type in (T_FILE, T_LFILE)

    @property
    def is_symlink(self) -> bool:
        return self.type in (T_SYMLINK, T_LSYMLINK)


class SquashFS:
    """Reader over a binary file object; `offset` allows reading an image
    embedded in another file (e.g. modloop inside an ISO)."""

    def __init__(self, fileobj, offset: int = 0):
        self.f = fileobj
        self.base = offset
        sb = self._read(0, 96)
        (magic, self.inode_count, _mtime, self.block_size, _frags, comp, _blog,
         self.flags, _ids, vmaj, vmin, self.root_ref, self.bytes_used, _id_tab,
         _xattr, self.inode_table, self.dir_table, self.frag_table,
         _export) = struct.unpack("<IIIIIHHHHHHQQQQQQQQ", sb)
        if magic != MAGIC:
            raise ValueError("not a squashfs image")
        if (vmaj, vmin) != (4, 0):
            raise ValueError(f"unsupported squashfs version {vmaj}.{vmin}")
        self.compressor_id = comp
        self._decompress = _decompressor(comp)
        self._meta_cache: dict[int, tuple[bytes, int]] = {}
        self._frag_entries: list[tuple[int, int]] | None = None
        self._frag_count = _frags

    # -- low level -------------------------------------------------------
    def _read(self, pos: int, size: int) -> bytes:
        self.f.seek(self.base + pos)
        data = self.f.read(size)
        if len(data) != size:
            raise EOFError("short read in squashfs image")
        return data

    def _meta_block(self, pos: int) -> tuple[bytes, int]:
        """Return (uncompressed data, position of next block)."""
        hit = self._meta_cache.get(pos)
        if hit:
            return hit
        (hdr,) = struct.unpack("<H", self._read(pos, 2))
        size = hdr & 0x7FFF
        raw = self._read(pos + 2, size)
        data = raw if hdr & 0x8000 else self._decompress(raw)
        res = (data, pos + 2 + size)
        self._meta_cache[pos] = res
        return res

    def _meta_read(self, pos: int, offset: int, size: int) -> tuple[bytes, int, int]:
        """Read `size` bytes of a metadata stream starting at block `pos`,
        byte `offset`. Returns (data, new_pos, new_offset)."""
        out = bytearray()
        while len(out) < size:
            data, nxt = self._meta_block(pos)
            chunk = data[offset:offset + size - len(out)]
            out += chunk
            offset += len(chunk)
            if offset >= len(data):
                pos, offset = nxt, 0
        return bytes(out), pos, offset

    # -- inodes ----------------------------------------------------------
    def inode(self, ref: int) -> Inode:
        pos = self.inode_table + (ref >> 16)
        off = ref & 0xFFFF
        hdr, pos, off = self._meta_read(pos, off, 16)
        itype, perm, _uid, _gid, mtime, number = struct.unpack("<HHHHII", hdr)
        ino = Inode(type=itype, mode=perm, mtime=mtime, number=number)
        if itype == T_DIR:
            d, pos, off = self._meta_read(pos, off, 16)
            blk, _links, fsize, boff, _parent = struct.unpack("<IIHHI", d)
            ino.dir_block, ino.size, ino.dir_offset = blk, fsize, boff
        elif itype == T_LDIR:
            d, pos, off = self._meta_read(pos, off, 24)
            _links, fsize, blk, _parent, _icount, boff, _x = struct.unpack("<IIIIHHI", d)
            ino.dir_block, ino.size, ino.dir_offset = blk, fsize, boff
        elif itype in (T_FILE, T_LFILE):
            if itype == T_FILE:
                d, pos, off = self._meta_read(pos, off, 16)
                start, frag, foff, fsize = struct.unpack("<IIII", d)
            else:
                d, pos, off = self._meta_read(pos, off, 40)
                start, fsize, _sparse, _links, frag, foff, _x = struct.unpack("<QQQIIII", d)
            nblocks = fsize // self.block_size
            if frag == NO_FRAGMENT and fsize % self.block_size:
                nblocks += 1
            sizes, pos, off = self._meta_read(pos, off, 4 * nblocks)
            ino.size, ino.blocks_start, ino.fragment, ino.frag_offset = fsize, start, frag, foff
            ino.block_sizes = list(struct.unpack(f"<{nblocks}I", sizes))
        elif itype in (T_SYMLINK, T_LSYMLINK):
            d, pos, off = self._meta_read(pos, off, 8)
            _links, tsize = struct.unpack("<II", d)
            target, pos, off = self._meta_read(pos, off, tsize)
            ino.target = target.decode("utf-8", errors="replace")
        return ino

    # -- directories -----------------------------------------------------
    def listdir(self, ino: Inode) -> dict[str, int]:
        """Map entry name -> inode reference."""
        if not ino.is_dir:
            raise NotADirectoryError
        entries: dict[str, int] = {}
        remaining = ino.size - 3  # size includes the implicit '.' and '..'
        pos, off = self.dir_table + ino.dir_block, ino.dir_offset
        while remaining > 0:
            hdr, pos, off = self._meta_read(pos, off, 12)
            count, start, _inum = struct.unpack("<III", hdr)
            remaining -= 12
            for _ in range(count + 1):
                e, pos, off = self._meta_read(pos, off, 8)
                ioff, _delta, _etype, nsize = struct.unpack("<HhHH", e)
                name, pos, off = self._meta_read(pos, off, nsize + 1)
                remaining -= 8 + nsize + 1
                entries[name.decode("utf-8", errors="replace")] = (start << 16) | ioff
        return entries

    def lookup(self, path: str) -> Inode:
        ino = self.inode(self.root_ref)
        for part in [p for p in path.strip("/").split("/") if p]:
            refs = self.listdir(ino)
            if part not in refs:
                raise FileNotFoundError(path)
            ino = self.inode(refs[part])
        return ino

    def walk(self, path: str = "/"):
        """Yield (path, Inode) for every entry below `path` (depth first)."""
        stack = [(path.rstrip("/") or "", self.lookup(path))]
        while stack:
            base, ino = stack.pop()
            for name, ref in sorted(self.listdir(ino).items(), reverse=True):
                child = self.inode(ref)
                p = f"{base}/{name}"
                yield p, child
                if child.is_dir:
                    stack.append((p, child))

    # -- file data -------------------------------------------------------
    def _fragment(self, index: int) -> tuple[int, int]:
        if self._frag_entries is None:
            nptr = (self._frag_count + 511) // 512
            ptrs = struct.unpack(f"<{nptr}Q", self._read(self.frag_table, 8 * nptr))
            entries = []
            for p in ptrs:
                data, _ = self._meta_block(p)
                for i in range(0, len(data), 16):
                    start, size, _unused = struct.unpack_from("<QII", data, i)
                    entries.append((start, size))
            self._frag_entries = entries
        return self._frag_entries[index]

    def _data_block(self, pos: int, size_field: int) -> bytes:
        size = size_field & 0xFFFFFF
        raw = self._read(pos, size)
        return raw if size_field & 0x1000000 else self._decompress(raw)

    def read_file(self, ino: Inode) -> bytes:
        if not ino.is_file:
            raise IsADirectoryError
        out = bytearray()
        pos = ino.blocks_start
        for sf in ino.block_sizes:
            if sf == 0:  # sparse block
                out += bytes(min(self.block_size, ino.size - len(out)))
                continue
            out += self._data_block(pos, sf)
            pos += sf & 0xFFFFFF
        if ino.fragment != NO_FRAGMENT:
            start, size = self._fragment(ino.fragment)
            frag = self._data_block(start, size)
            out += frag[ino.frag_offset:ino.frag_offset + ino.size - len(out)]
        return bytes(out[:ino.size])

    def file_mode(self, ino: Inode) -> int:
        kind = stat.S_IFDIR if ino.is_dir else stat.S_IFLNK if ino.is_symlink else stat.S_IFREG
        return kind | ino.mode
