"""Build the Alpine apkovl (.apkovl.tar.gz) with Python's tarfile.

All entries are owned by root:root with explicit permissions, so the result
does not depend on the Windows file system the sources live on.
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
from dataclasses import dataclass, field

EXEC_DIRS = ("etc/init.d/", "usr/local/bin/", "usr/local/sbin/", "etc/local.d/",
             "etc/udhcpc/", "usr/share/udhcpc/")
TEXT_SUFFIXES = ("", ".sh", ".conf", ".cfg", ".py", ".toml", ".txt", ".service")


class OverlayError(Exception):
    pass


@dataclass
class Overlay:
    mtime: int
    entries: dict[str, tuple] = field(default_factory=dict)

    def _parents(self, path: str) -> None:
        parts = path.split("/")[:-1]
        for k in range(1, len(parts) + 1):
            d = "/".join(parts[:k])
            if d not in self.entries:
                self.entries[d] = ("dir", 0o755)

    def dir(self, path: str, mode: int = 0o755) -> None:
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = ("dir", mode)

    def file(self, path: str, data: bytes, mode: int = 0o644) -> None:
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = ("file", mode, data)

    def symlink(self, path: str, target: str) -> None:
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = ("link", 0o777, target)

    def add_tree(self, src: str, dest: str = "", mode_for=None) -> None:
        """Copy a directory tree; text files must use LF line endings."""
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel_root = os.path.relpath(root, src).replace(os.sep, "/")
            rel_root = "" if rel_root == "." else rel_root
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                rel = f"{rel_root}/{name}" if rel_root else name
                target = f"{dest.strip('/')}/{rel}" if dest else rel
                with open(os.path.join(root, name), "rb") as f:
                    data = f.read()
                if os.path.splitext(name)[1] in TEXT_SUFFIXES and b"\r\n" in data:
                    raise OverlayError(f"{target}: CRLF line endings (check .gitattributes)")
                mode = mode_for(target) if mode_for else default_mode(target)
                self.file(target, data, mode)

    def tar_gz(self) -> bytes:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=self.mtime) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as t:
                for path in sorted(self.entries):
                    kind, mode, *rest = self.entries[path]
                    ti = tarfile.TarInfo(path)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mtime = self.mtime
                    ti.mode = mode
                    if kind == "dir":
                        ti.type = tarfile.DIRTYPE
                        t.addfile(ti)
                    elif kind == "link":
                        ti.type = tarfile.SYMTYPE
                        ti.linkname = rest[0]
                        t.addfile(ti)
                    else:
                        ti.size = len(rest[0])
                        t.addfile(ti, io.BytesIO(rest[0]))
        return buf.getvalue()


def default_mode(path: str) -> int:
    if path.startswith(EXEC_DIRS):
        return 0o755
    if path.endswith((".secret", "shadow")):
        return 0o600
    return 0o644
