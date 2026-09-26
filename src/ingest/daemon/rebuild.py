"""Rebuild the index from the manifests found on the share.

Every import folder written with verification contains `<folder>.xxh128`
(one "hash  relative/path" line per file). For each listed file still on
the share, the fingerprint is recomputed from its edges; import folders
already present in the index are skipped. Imports made without
verification have no manifest and cannot be recovered this way.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading

from . import fingerprint
from .copier import Cancelled
from .index import Index

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})")


def parse_manifest(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        h, sep, rel = line.partition("  ")
        if sep and len(h) == 32 and all(c in "0123456789abcdef" for c in h.lower()):
            out.append((h.lower(), rel.strip()))
    return out


def folder_time(name: str, path: str) -> float:
    m = _DATE.match(name)
    if m:
        try:
            return dt.datetime(*map(int, m.groups())).timestamp()
        except ValueError:
            pass
    return os.path.getmtime(path)


def rebuild(index: Index, share_root: str, cancel: threading.Event | None = None,
            progress=None) -> dict:
    known = {h["dest_folder"] for h in index.history(limit=1_000_000)}
    stats = {"folders": 0, "files": 0, "missing": 0, "skipped_folders": 0}
    for name in sorted(os.listdir(share_root)):
        folder = os.path.join(share_root, name)
        manifest = os.path.join(folder, f"{name}.xxh128")
        if not os.path.isfile(manifest):
            continue
        if name in known:
            stats["skipped_folders"] += 1
            continue
        with open(manifest, "rb") as f:
            entries = parse_manifest(f.read().decode("utf-8", errors="replace"))
        started = folder_time(name, folder)
        imp = index.start_import(dest_folder=name, verify_mode="hash", kind="rebuilt",
                                 files_total=len(entries))
        for h, rel in entries:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            path = os.path.join(folder, *rel.split("/"))
            try:
                st = os.stat(path)
                fp = fingerprint.of_file(path, st.st_size)
            except OSError:
                stats["missing"] += 1
                continue
            index.add_file(imp, fp, rel, st.st_size, st.st_mtime, h, status="copied")
            stats["files"] += 1
        index.finish_import(imp, "done")
        index.set_started(imp, started)
        stats["folders"] += 1
        if progress:
            progress(dict(stats, current=name))
    return stats
