"""Scan a mounted source and compare it with the index.

Only files whose size was seen in a past import can be already imported,
so the edges (first/last MiB) are read just for those; brand-new cards are
summarised without reading their content.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from ingest.common import sysfs

from . import fingerprint, naming
from .copier import Cancelled, Item
from .index import Index

# system folders and files never imported
SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", ".Trashes", ".Spotlight-V100",
             ".fseventsd", ".TemporaryItems", "LOST.DIR", "lost+found", ".ingest-station"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".metadata_never_index"}


@dataclass
class FileEntry:
    item: Item
    fp: str | None = None
    known_import: int | None = None


@dataclass
class ScanResult:
    files: list[FileEntry] = field(default_factory=list)
    total_bytes: int = 0
    known_files: int = 0
    known_bytes: int = 0
    previous: list[dict] = field(default_factory=list)   # [{import_id, folder, date, files}]
    seconds: float = 0.0
    brand: str = ""                                       # camera brand guessed from DCIM

    @property
    def new_files(self) -> int:
        return len(self.files) - self.known_files

    @property
    def new_bytes(self) -> int:
        return self.total_bytes - self.known_bytes

    def plan(self, mode: str) -> list[Item]:
        """Items to copy: 'new' (default) or 'all'."""
        if mode == "all":
            return [f.item for f in self.files]
        return [f.item for f in self.files if f.known_import is None]

    def summary(self) -> dict:
        return {"files": len(self.files), "bytes": self.total_bytes,
                "known_files": self.known_files, "known_bytes": self.known_bytes,
                "new_files": self.new_files, "new_bytes": self.new_bytes,
                "previous": self.previous, "scan_seconds": round(self.seconds, 1),
                "brand": self.brand}


def walk(root: str, cancel: threading.Event | None = None) -> list[Item]:
    """All regular files below root, sorted, with sanitised relative paths."""
    items = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_FILES or name.startswith("._"):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, root)
            # names with invalid bytes: keep the real path for reading,
            # use replacement characters for the destination
            shown = sysfs.decode(os.fsencode(rel))
            items.append(Item(src=full, rel=naming.sanitize_relpath(shown),
                              size=st.st_size, mtime=st.st_mtime))
    return items


def _raise(err: OSError) -> None:
    raise err


def scan(root: str, index: Index, cancel: threading.Event | None = None) -> ScanResult:
    """Scan a mounted file system."""
    return scan_items(walk(root, cancel), index, cancel,
                      brand=naming.dcim_brand(sysfs.listdir(os.path.join(root, "DCIM"))))


def scan_items(items: list[Item], index: Index, cancel: threading.Event | None = None,
               brand: str = "") -> ScanResult:
    """Compare a list of files (from a card or a camera) with the index."""
    t0 = time.monotonic()
    res = ScanResult()
    res.brand = brand
    sizes = index.known_sizes()
    fps: list[str] = []
    for it in items:
        entry = FileEntry(item=it)
        res.files.append(entry)
        res.total_bytes += it.size
        if it.size in sizes:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            entry.fp = fingerprint.of_file(it.src, it.size, it.opener)
            fps.append(entry.fp)
    known = index.lookup(fps) if fps else {}
    prev: dict[int, dict] = {}
    for entry in res.files:
        k = known.get(entry.fp) if entry.fp else None
        if k is None:
            continue
        entry.known_import = k.import_id
        res.known_files += 1
        res.known_bytes += entry.item.size
        p = prev.setdefault(k.import_id, {"import_id": k.import_id, "folder": k.dest_folder,
                                          "date": k.started, "files": 0})
        p["files"] += 1
    res.previous = sorted(prev.values(), key=lambda p: p["date"])
    res.seconds = time.monotonic() - t0
    return res
