"""SQLite index of everything that has been imported.

Recognition is per file (fingerprint), not per card: it works after the card
has been formatted, with another reader, or after an import from the camera
cable. The index records the import history, not the current content of
the share: a file deleted from the share is still "already imported";
"Import all" forces the copy.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,            -- running, done, failed, cancelled, interrupted
    source_label TEXT, source_model TEXT, source_serial TEXT, source_kind TEXT,
    dest_folder TEXT NOT NULL,       -- relative to the share root
    verify_mode TEXT NOT NULL,
    files_total INTEGER DEFAULT 0, bytes_total INTEGER DEFAULT 0,
    files_done INTEGER DEFAULT 0, bytes_done INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    fp TEXT NOT NULL,
    rel_path TEXT NOT NULL,          -- path inside the import folder
    size INTEGER NOT NULL,
    mtime REAL,
    xxh128 TEXT,
    status TEXT NOT NULL,            -- copied, verified
    done_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_fp ON files(fp);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE INDEX IF NOT EXISTS files_import ON files(import_id);
"""


@dataclass
class Known:
    fp: str
    import_id: int
    dest_folder: str
    started: float


class Index:
    """Thread-safe wrapper (one connection per thread, WAL journal)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # -- imports ---------------------------------------------------------
    def start_import(self, *, dest_folder: str, verify_mode: str, label: str = "",
                     model: str = "", serial: str = "", kind: str = "",
                     files_total: int = 0, bytes_total: int = 0) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO imports(started, status, source_label, source_model, source_serial,"
                " source_kind, dest_folder, verify_mode, files_total, bytes_total)"
                " VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), label, model, serial, kind, dest_folder, verify_mode,
                 files_total, bytes_total))
            return int(cur.lastrowid)

    def finish_import(self, import_id: int, status: str, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE imports SET finished=?, status=?, error=?,"
                      " files_done=(SELECT COUNT(*) FROM files WHERE import_id=?),"
                      " bytes_done=(SELECT COALESCE(SUM(size),0) FROM files WHERE import_id=?)"
                      " WHERE id=?", (time.time(), status, error, import_id, import_id, import_id))

    def set_started(self, import_id: int, started: float) -> None:
        with self._conn() as c:
            c.execute("UPDATE imports SET started=? WHERE id=?", (started, import_id))

    def reset(self) -> int:
        """Forget every import (the copies on the share are untouched).
        Returns the number of imports removed."""
        with self._conn() as c:
            c.execute("DELETE FROM files")
            n = c.execute("DELETE FROM imports").rowcount
        self._conn().execute("VACUUM")
        return n

    def mark_interrupted(self) -> int:
        """At daemon start: imports left 'running' were interrupted."""
        with self._conn() as c:
            return c.execute("UPDATE imports SET status='interrupted', finished=?"
                             " WHERE status='running'", (time.time(),)).rowcount

    def get_import(self, import_id: int) -> dict | None:
        row = self._conn().execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
        return dict(row) if row else None

    def history(self, limit: int = 50, query: str = "") -> list[dict]:
        sql = "SELECT * FROM imports"
        args: list = []
        if query:
            sql += (" WHERE source_label LIKE ? OR source_model LIKE ? OR dest_folder LIKE ?"
                    " OR date(started, 'unixepoch', 'localtime') LIKE ?")
            args = [f"%{query}%"] * 4
        sql += " ORDER BY started DESC LIMIT ?"
        return [dict(r) for r in self._conn().execute(sql, args + [limit])]

    # -- files -----------------------------------------------------------
    def add_file(self, import_id: int, fp: str, rel_path: str, size: int, mtime: float,
                 xxh128: str | None, status: str = "copied") -> None:
        """Record a completed file (one transaction per file: an interrupted
        import resumes after the last recorded file)."""
        with self._conn() as c:
            c.execute("INSERT INTO files(import_id, fp, rel_path, size, mtime, xxh128, status,"
                      " done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (import_id, fp, rel_path, size, mtime, xxh128, status, time.time()))

    def mark_verified(self, import_id: int, rel_paths: list[str]) -> None:
        with self._conn() as c:
            c.executemany("UPDATE files SET status='verified' WHERE import_id=? AND rel_path=?",
                          [(import_id, p) for p in rel_paths])

    def files_of(self, import_id: int) -> list[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM files WHERE import_id=? ORDER BY id", (import_id,))]

    def known_sizes(self) -> set[int]:
        """All sizes ever imported: a file whose size is not here is new
        without reading it."""
        return {r[0] for r in self._conn().execute("SELECT DISTINCT size FROM files")}

    def lookup(self, fps: list[str]) -> dict[str, Known]:
        """Earliest import of each fingerprint."""
        out: dict[str, Known] = {}
        c = self._conn()
        for i in range(0, len(fps), 500):
            chunk = fps[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in c.execute(
                    f"SELECT f.fp, i.id, i.dest_folder, i.started FROM files f"
                    f" JOIN imports i ON i.id = f.import_id WHERE f.fp IN ({marks})"
                    f" ORDER BY i.started", chunk):
                out.setdefault(r[0], Known(r[0], r[1], r[2], r[3]))
        return out

    # -- backup ----------------------------------------------------------
    def backup_to(self, dest: str) -> None:
        """Consistent copy of the database. The SQLite backup is made next to
        the local database, then copied as a plain file: SQLite must never
        open files on a network share (its locks can hang on SMB/NFS)."""
        local = self.path + ".backup"
        target = sqlite3.connect(local)
        try:
            self._conn().backup(target)
        finally:
            target.close()
        tmp = dest + ".tmp"
        with open(local, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dest)
        os.remove(local)
