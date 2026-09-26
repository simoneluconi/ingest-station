"""Destination folder names and file name sanitisation.

Names must be valid on every share type (NFS exports of Linux file systems,
SMB shares backed by NTFS-like semantics), so the Windows rules are applied
everywhere: no reserved characters, no control characters, no trailing dots
or spaces, no reserved device names.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 200  # bytes; leaves room for the temporary suffix under 255

DEFAULT_SCHEMA = "{date}_{time}_{label}"


def sanitize(name: str, fallback: str = "_") -> str:
    """Make one path component safe for any share."""
    name = unicodedata.normalize("NFC", name)
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        name = fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if not dot or len(ext) > 16:
            stem, ext = name, ""
        keep = MAX_NAME - len(ext.encode()) - (1 if ext else 0)
        stem = stem.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
        name = f"{stem}.{ext}" if ext else stem
    return name


def sanitize_relpath(rel: str) -> str:
    """Sanitise every component of a relative path (separators: / or \\)."""
    parts = [p for p in re.split(r"[/\\]+", rel) if p and p != "."]
    return "/".join(sanitize(p) for p in parts if p != "..")


def source_label(label: str | None, model: str | None, fallback: str = "Source") -> str:
    """Folder label: volume label if meaningful, else the device model."""
    for cand in (label, model):
        cand = (cand or "").strip()
        if cand and cand.upper() not in ("NO NAME", "UNTITLED", "EOS_DIGITAL_"):
            return sanitize(cand).replace(" ", "_")
    return fallback


def folder_name(when: dt.datetime, label: str, schema: str = DEFAULT_SCHEMA) -> str:
    """Destination folder, default AAAA-MM-GG_HHMM_<label>."""
    values = {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H%M"),
              "label": label, "year": when.strftime("%Y"), "month": when.strftime("%m")}
    try:
        name = schema.format(**values)
    except (KeyError, IndexError, ValueError):
        name = DEFAULT_SCHEMA.format(**values)
    return sanitize(name)


def unique_folder(name: str, exists) -> str:
    """Append _2, _3... while `exists(candidate)` is true."""
    if not exists(name):
        return name
    n = 2
    while exists(f"{name}_{n}"):
        n += 1
    return f"{name}_{n}"
