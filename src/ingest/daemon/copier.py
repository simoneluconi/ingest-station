"""Copy engine: maximum throughput with little CPU on the Atom.

Each file is streamed by a reader thread and a writer (the calling thread)
through a small pool of large reusable buffers, so reading from the card and
writing to the network overlap. Data goes to a temporary file that is
fsync'ed, gets the original mtime and is atomically renamed. The xxh128 of
the data and the fingerprint edges are computed on the way.

Errors are classified: SourceError (card removed, read error) ends the
import; DestError (share gone, disk full) pauses it until Retry.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass

import xxhash

from .fingerprint import EdgeCollector

CHUNK = 4 << 20
POOL = 4
TMP_SUFFIX = ".ingest-part"


class CopyError(Exception):
    pass


class SourceError(CopyError):
    """Reading the source failed (removed card, I/O error, file changed)."""


class DestError(CopyError):
    """Writing the destination failed (share offline, full, permissions)."""


class Cancelled(Exception):
    pass


# errno values meaning "the network share is gone"
_SHARE_GONE = {5, 11, 110, 112, 113, 107, 116, 103, 104}  # EIO EAGAIN ETIMEDOUT EHOSTDOWN
#                                                         EHOSTUNREACH ENOTCONN ESTALE
#                                                         ECONNABORTED ECONNRESET


def dest_message(action: str, rel: str, e: OSError) -> str:
    """Human message for a destination failure."""
    if e.errno in _SHARE_GONE:
        return f"the share stopped responding ({action} {rel})"
    if e.errno == 28:
        return "the share is full"
    if e.errno in (1, 13, 30):
        return f"permission denied on the share ({action} {rel})"
    return f"cannot {action} {rel}: {e.strerror or e}"


@dataclass
class Item:
    src: str          # absolute path on the mounted source (may hold surrogates)
    rel: str          # sanitised destination path, relative to the import folder
    size: int
    mtime: float


@dataclass
class Result:
    fp: str
    xxh128: str | None


def _fadvise(fd: int, advice_name: str, offset: int = 0, length: int = 0) -> None:
    advice = getattr(os, advice_name, None)
    if advice is not None and hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, offset, length, advice)
        except OSError:
            pass


def temp_path(dst: str) -> str:
    d, name = os.path.split(dst)
    return os.path.join(d, f".{name}{TMP_SUFFIX}")


def copy_file(item: Item, dest_root: str, *, want_hash: bool = True, progress=None,
              cancel: threading.Event | None = None, chunk: int = CHUNK) -> Result:
    """Copy one file; `progress(nbytes)` is called for every chunk written."""
    dst = os.path.join(dest_root, *item.rel.split("/"))
    tmp = temp_path(dst)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        fout = open(tmp, "wb", buffering=0)
    except OSError as e:
        raise DestError(dest_message("create", item.rel, e)) from e
    try:
        try:
            fin = open(item.src, "rb", buffering=0)
        except OSError as e:
            raise SourceError(f"cannot open {item.rel}: {e.strerror or e}") from e
        with fin:
            _fadvise(fin.fileno(), "POSIX_FADV_SEQUENTIAL")
            h = xxhash.xxh3_128() if want_hash else None
            edges = EdgeCollector(item.size)
            total = _stream(fin, fout, h, edges, progress, cancel, chunk, item.rel)
        if total != item.size:
            raise SourceError(f"{item.rel} changed while copying ({total} of {item.size} bytes)")
        try:
            os.fsync(fout.fileno())
            _fadvise(fout.fileno(), "POSIX_FADV_DONTNEED")
        except OSError as e:
            raise DestError(dest_message("write", item.rel, e)) from e
    except BaseException:
        fout.close()
        _remove_quietly(tmp)
        raise
    try:
        fout.close()
        os.utime(tmp, (item.mtime, item.mtime))
        os.replace(tmp, dst)
    except OSError as e:
        _remove_quietly(tmp)
        raise DestError(dest_message("finish", item.rel, e)) from e
    return Result(fp=edges.fingerprint(), xxh128=h.hexdigest() if h else None)


def _stream(fin, fout, h, edges, progress, cancel, chunk, rel) -> int:
    if 0 < os.fstat(fin.fileno()).st_size <= chunk:
        # small file: no thread
        data = _read_all(fin, rel)
        _write_all(fout, memoryview(data), rel)
        if h:
            h.update(data)
        edges.update(data)
        if progress:
            progress(len(data))
        return len(data)

    free: queue.Queue = queue.Queue()
    for _ in range(POOL):
        free.put(bytearray(chunk))
    full: queue.Queue = queue.Queue()
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                buf = free.get()
                if buf is None:
                    break
                try:
                    n = fin.readinto(buf)
                except OSError as e:
                    full.put(SourceError(f"read error in {rel}: {e.strerror or e}"))
                    return
                full.put((buf, n))
                if not n:
                    return
        except BaseException as e:  # pragma: no cover - defensive
            full.put(SourceError(f"read error in {rel}: {e}"))

    t = threading.Thread(target=reader, name="ingest-reader", daemon=True)
    t.start()
    total = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            got = full.get()
            if isinstance(got, Exception):
                raise got
            buf, n = got
            if not n:
                break
            mv = memoryview(buf)[:n]
            _write_all(fout, mv, rel)
            if h:
                h.update(mv)
            edges.update(mv)
            total += n
            if progress:
                progress(n)
            free.put(buf)
    finally:
        stop.set()
        free.put(None)
        t.join(timeout=30)
    return total


def _read_all(fin, rel) -> bytes:
    try:
        parts = []
        while True:
            b = fin.read(CHUNK)
            if not b:
                return b"".join(parts)
            parts.append(b)
    except OSError as e:
        raise SourceError(f"read error in {rel}: {e.strerror or e}") from e


def _write_all(fout, mv: memoryview, rel: str) -> None:
    try:
        while mv:
            n = fout.write(mv)
            mv = mv[n:]
    except OSError as e:
        raise DestError(dest_message("write", rel, e)) from e


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# -- verification ------------------------------------------------------

def drop_caches() -> None:
    """Make re-reads come from the share, not from this machine's cache."""
    if hasattr(os, "sync"):
        os.sync()
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except OSError:
        pass


def hash_file(path: str, *, progress=None, cancel: threading.Event | None = None,
              chunk: int = CHUNK, rel: str = "") -> str:
    """xxh128 of a file read back from the destination."""
    h = xxhash.xxh3_128()
    try:
        with open(path, "rb", buffering=0) as f:
            _fadvise(f.fileno(), "POSIX_FADV_DONTNEED")
            _fadvise(f.fileno(), "POSIX_FADV_SEQUENTIAL")
            buf = bytearray(chunk)
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                n = f.readinto(buf)
                if not n:
                    break
                h.update(memoryview(buf)[:n])
                if progress:
                    progress(n)
    except OSError as e:
        raise DestError(dest_message("read back", rel or path, e)) from e
    return h.hexdigest()
