"""GUI assets built on the PC: the Inter font faces and vendored pure-Python
wheels (QR codes)."""

from __future__ import annotations

import hashlib
import io
import zipfile

from fontTools import subset
from fontTools.ttLib import TTCollection

# Inter (Rasmus Andersson), SIL Open Font License 1.1
FACES = {"Inter Regular": "Inter-Regular.ttf", "Inter Medium": "Inter-Medium.ttf",
         "Inter SemiBold": "Inter-SemiBold.ttf", "Inter Bold": "Inter-Bold.ttf"}
# Latin, Latin-1, Latin Extended-A, punctuation, arrows, a few symbols
UNICODES = (list(range(0x20, 0x7F)) + list(range(0xA0, 0x180)) + list(range(0x2010, 0x2070))
            + list(range(0x2190, 0x21A0)) + [0x20AC, 0x2122, 0x2212, 0x2713, 0x2715, 0x00D7,
                                              0x2026, 0x25CF, 0x2022, 0x2248, 0x2264, 0x2265])
FONT_NOTE = (b"Inter typeface by Rasmus Andersson (https://rsms.me/inter/), licensed under\n"
             b"the SIL Open Font License 1.1. Subset of the Alpine font-inter package.\n")


class AssetError(Exception):
    pass


def inter_fonts(ttc_bytes: bytes) -> dict[str, bytes]:
    """Extract and subset the needed static faces from Inter.ttc."""
    coll = TTCollection(io.BytesIO(ttc_bytes))
    out: dict[str, bytes] = {}
    for font in coll.fonts:
        full = font["name"].getDebugName(4) or ""
        if full not in FACES:
            continue
        opts = subset.Options()
        opts.layout_features = ["kern", "liga", "calt", "tnum", "case"]
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        sub = subset.Subsetter(opts)
        sub.populate(unicodes=UNICODES)
        sub.subset(font)
        buf = io.BytesIO()
        font.save(buf)
        out[FACES[full]] = buf.getvalue()
    missing = set(FACES.values()) - set(out)
    if missing:
        raise AssetError(f"Inter faces not found in the collection: {sorted(missing)}")
    return out


def wheel_files(data: bytes, sha256: str, package: str) -> dict[str, bytes]:
    """Files of `package` from a pure-Python wheel with a pinned hash."""
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AssetError(f"{package}: wheel checksum mismatch")
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.startswith(package + "/") and not name.endswith("/"):
                out[name] = z.read(name)
    if not out:
        raise AssetError(f"{package}: package not found in the wheel")
    return out
