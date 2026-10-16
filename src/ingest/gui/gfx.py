"""Drawing primitives with anti-aliasing and caches: rounded rectangles
(supersampled corners), soft shadows, cached text, vector icons, progress
bars and QR codes. pygame's own rounded rectangles are aliased; these are
smooth and cheap once cached."""

from __future__ import annotations

import collections
import math
import struct
import zlib

import pygame
import pygame.freetype

SS = 4   # supersampling factor for corners and icons


class LRU(collections.OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def get_or(self, key, make):
        if key in self:
            self.move_to_end(key)
            return self[key]
        val = make()
        self[key] = val
        if len(self) > self.size:
            self.popitem(last=False)
        return val


_corners = LRU(256)
_shadows = LRU(64)
_texts = LRU(1500)
_icons = LRU(256)
_fonts: dict = {}


def _clear_surface(size, color) -> pygame.Surface:
    """A transparent surface whose pixels already have `color`: smoothscale
    averages RGB too, and the default transparent *black* would darken every
    anti-aliased edge (a grey halo around rounded corners and icons)."""
    surf = pygame.Surface(size, pygame.SRCALPHA)
    surf.fill((*color[:3], 0))
    return surf


def _corner(radius: int, color) -> pygame.Surface:
    """Top-left quarter disc of `radius`, anti-aliased, in `color`."""
    def make():
        big = _clear_surface((radius * SS, radius * SS), (255, 255, 255))
        pygame.draw.circle(big, (255, 255, 255, 255), (radius * SS, radius * SS), radius * SS)
        mask = pygame.transform.smoothscale(big, (radius, radius))
        surf = pygame.Surface((radius, radius), pygame.SRCALPHA)
        surf.fill((*color[:3], 255))
        surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        return surf
    return _corners.get_or((radius, tuple(color[:3])), make)


def rrect(surf: pygame.Surface, rect, color, radius: int) -> None:
    """Filled rounded rectangle with smooth corners."""
    r = pygame.Rect(rect)
    radius = max(0, min(radius, r.w // 2, r.h // 2))
    if radius < 2:
        pygame.draw.rect(surf, color, r)
        return
    pygame.draw.rect(surf, color, (r.x + radius, r.y, r.w - 2 * radius, r.h))
    pygame.draw.rect(surf, color, (r.x, r.y + radius, radius, r.h - 2 * radius))
    pygame.draw.rect(surf, color, (r.right - radius, r.y + radius, radius, r.h - 2 * radius))
    c = _corner(radius, color)
    surf.blit(c, (r.x, r.y))
    surf.blit(pygame.transform.flip(c, True, False), (r.right - radius, r.y))
    surf.blit(pygame.transform.flip(c, False, True), (r.x, r.bottom - radius))
    surf.blit(pygame.transform.flip(c, True, True), (r.right - radius, r.bottom - radius))


def rrect_border(surf, rect, fill, border, radius: int, width: int) -> None:
    r = pygame.Rect(rect)
    rrect(surf, r, border, radius)
    rrect(surf, r.inflate(-2 * width, -2 * width), fill, max(0, radius - width))


def shadow(surf, rect, radius: int, color, alpha: int, blur: int, offset_y: int = 0) -> None:
    """Soft drop shadow (a small rounded rectangle blurred by up-scaling)."""
    r = pygame.Rect(rect)
    key = (r.w, r.h, radius, tuple(color[:3]), alpha, blur)

    def make():
        k = 6
        w, h = r.w + blur * 2, r.h + blur * 2
        small = _clear_surface((max(1, w // k), max(1, h // k)), color)
        inner = pygame.Rect(blur // k, blur // k, max(1, r.w // k), max(1, r.h // k))
        pygame.draw.rect(small, (*color[:3], alpha), inner, border_radius=max(1, radius // k))
        # two smooth passes approximate a gaussian blur
        mid = pygame.transform.smoothscale(small, (max(1, w // 3), max(1, h // 3)))
        return pygame.transform.smoothscale(mid, (w, h))
    s = _shadows.get_or(key, make)
    surf.blit(s, (r.x - blur, r.y - blur + offset_y))


def card(surf, rect, theme, radius: int | None = None, color=None, lift: int = 1) -> None:
    """Surface card with a soft shadow."""
    radius = theme.s(28) if radius is None else radius
    if lift:
        shadow(surf, rect, radius, theme.c["shadow"], theme.c["shadow_alpha"], theme.s(22),
               theme.s(6))
    rrect(surf, rect, color or theme.c["surface"], radius)


# -- text ----------------------------------------------------------------

def font(theme, weight: str) -> pygame.freetype.Font:
    path = theme.font_path(weight)
    f = _fonts.get(path)
    if f is None:
        try:
            f = pygame.freetype.Font(path)
        except (OSError, IOError):
            f = pygame.freetype.SysFont(None, 12)
        f.kerning = True
        # full line box (ascender to descender) instead of the ink bounds: the
        # anchors then line up whatever the glyphs ("Settings" vs "Back")
        f.pad = True
        _fonts[path] = f
    return f


def text_surface(theme, s: str, size, color, weight: str = "regular") -> pygame.Surface:
    px = theme.size(size) if isinstance(size, str) else theme.s(size)

    def make():
        surf, _ = font(theme, weight).render(s, fgcolor=color, size=px)
        return surf
    return _texts.get_or((s, px, tuple(color), weight, theme.font_path(weight)), make)


def fit(theme, s: str, size, weight: str, max_w: int) -> str:
    """Ellipsize `s` (in the middle for paths) to fit max_w."""
    if text_surface(theme, s, size, (0, 0, 0), weight).get_width() <= max_w or len(s) < 4:
        return s
    keep_end = "/" in s
    lo, hi = 1, len(s)
    best = "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = (s[:mid // 2] + "…" + s[-(mid - mid // 2):]) if keep_end else (s[:mid] + "…")
        if text_surface(theme, cand, size, (0, 0, 0), weight).get_width() <= max_w:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best


SIZE_ORDER = ["xs", "sm", "md", "lg", "xl", "xxl", "hero"]


def text(surf, theme, s: str, pos, size="md", color=None, weight="regular",
         anchor="topleft", max_w: int | None = None,
         min_size: str | None = None) -> pygame.Rect:
    """Draw `s`. With max_w, a text that does not fit is first drawn smaller,
    down to min_size (named sizes only), and only then ellipsized."""
    color = color or theme.c["text"]
    if max_w and min_size and size in SIZE_ORDER:
        for smaller in SIZE_ORDER[SIZE_ORDER.index(min_size):SIZE_ORDER.index(size) + 1][::-1]:
            size = smaller
            if text_width(theme, s, size, weight) <= max_w:
                break
    if max_w:
        s = fit(theme, s, size, weight, max_w)
    t = text_surface(theme, s, size, color, weight)
    if anchor in ("baseline", "baseline_right"):
        # pos is on the baseline: lines up texts of different sizes ("12 files")
        r = t.get_rect()
        r.top = pos[1] - ascender(theme, size, weight)
        if anchor == "baseline":
            r.left = pos[0]
        else:
            r.right = pos[0]
    else:
        r = t.get_rect(**{anchor: pos})
    surf.blit(t, r)
    return r


def ascender(theme, size="md", weight="regular") -> int:
    """Distance from the top of a text surface to its baseline."""
    px = theme.size(size) if isinstance(size, str) else theme.s(size)
    return font(theme, weight).get_sized_ascender(px)


def baseline(theme, rect: pygame.Rect, size="md", weight="regular") -> int:
    """The baseline y of a text drawn in `rect` (as returned by text())."""
    return rect.top + ascender(theme, size, weight)


def text_width(theme, s: str, size="md", weight="regular") -> int:
    return text_surface(theme, s, size, (0, 0, 0), weight).get_width()


def line_height(theme, size="md") -> int:
    return int((theme.size(size) if isinstance(size, str) else theme.s(size)) * 1.3)


# -- icons ---------------------------------------------------------------

def _poly(d, pts, color, s):
    pygame.draw.polygon(d, color, [(x * s, y * s) for x, y in pts])


def _draw_icon(d: pygame.Surface, name: str, s: float, col, lw: int) -> None:
    """Icons on a 24x24 grid, scale s; lw = stroke width (supersampled px)."""
    def L(*pts, w=lw):
        pygame.draw.lines(d, col, False, [(x * s, y * s) for x, y in pts], w)
        for x, y in (pts[0], pts[-1]):
            pygame.draw.circle(d, col, (x * s, y * s), w / 2)

    def R(x, y, w, h, r=2, width=lw):
        pygame.draw.rect(d, col, (x * s, y * s, w * s, h * s), width, border_radius=int(r * s))

    def C(x, y, r, width=lw):
        pygame.draw.circle(d, col, (x * s, y * s), r * s, width)

    if name == "card":            # SD card
        pts = [(6, 2.5), (15, 2.5), (19.5, 7), (19.5, 21.5), (6, 21.5)]
        pygame.draw.lines(d, col, True, [(x * s, y * s) for x, y in pts], lw)
        for x in (9, 12, 15):
            L((x, 5.5), (x, 8.5))
    elif name == "usb":           # USB stick
        R(7, 8, 10, 14, 2.5)
        R(9, 2.5, 6, 5.5, 1)
        L((11, 4.5), (11, 5.5))
        L((13, 4.5), (13, 5.5))
    elif name == "drive":
        R(3, 5, 18, 14, 3)
        C(16.5, 12, 1.2, 0)
        L((6.5, 12), (11, 12))
    elif name == "camera":
        R(2.5, 7, 19, 13.5, 3)
        pts = [(8, 7), (9.5, 4), (14.5, 4), (16, 7)]
        pygame.draw.lines(d, col, False, [(x * s, y * s) for x, y in pts], lw)
        C(12, 13.5, 4)
    elif name == "phone":
        R(6.5, 2.5, 11, 19, 2.5)
        L((10.5, 18.5), (13.5, 18.5))
    elif name == "check":
        L((5, 12.5), (10, 17.5), (19.5, 7))
    elif name == "cross":
        L((6.5, 6.5), (17.5, 17.5))
        L((17.5, 6.5), (6.5, 17.5))
    elif name == "warning":
        pts = [(12, 3), (22, 20.5), (2, 20.5)]
        pygame.draw.lines(d, col, True, [(x * s, y * s) for x, y in pts], lw)
        L((12, 9.5), (12, 14))
        C(12, 17.3, 1.1, 0)
    elif name == "gear":
        C(12, 12, 3.2)
        for k in range(8):
            a = k * math.pi / 4
            L((12 + 6.3 * math.cos(a), 12 + 6.3 * math.sin(a)),
              (12 + 9 * math.cos(a), 12 + 9 * math.sin(a)), w=int(lw * 1.6))
        C(12, 12, 6.8)
    elif name == "network":
        R(9, 3, 6, 5, 1)
        R(2.5, 16, 6, 5, 1)
        R(15.5, 16, 6, 5, 1)
        L((12, 8), (12, 12))
        L((5.5, 16), (5.5, 12), (18.5, 12), (18.5, 16))
    elif name == "folder":
        pts = [(2.5, 6), (2.5, 19.5), (21.5, 19.5), (21.5, 8), (11.5, 8), (9.5, 5), (2.5, 5)]
        pygame.draw.lines(d, col, True, [(x * s, y * s) for x, y in pts], lw)
    elif name == "back":
        L((15, 5), (8, 12), (15, 19))
    elif name == "chevron":
        L((9, 5), (16, 12), (9, 19))
    elif name == "down":
        L((5, 9), (12, 16), (19, 9))
    elif name == "eject":
        _poly(d, [(12, 4), (20, 13), (4, 13)], col, s)
        R(4, 16, 16, 3, 1, 0)
    elif name == "power":
        pygame.draw.arc(d, col, (4.5 * s, 4.5 * s, 15 * s, 15 * s), math.radians(120),
                        math.radians(420), lw)
        L((12, 2.5), (12, 11))
    elif name == "refresh":
        pygame.draw.arc(d, col, (4 * s, 4 * s, 16 * s, 16 * s), math.radians(20),
                        math.radians(300), lw)
        _poly(d, [(20.5, 3), (21, 10), (14.5, 8)], col, s)
    elif name == "clock":
        C(12, 12, 9)
        L((12, 7), (12, 12), (15.5, 14))
    elif name == "sun":
        C(12, 12, 4)
        for k in range(8):
            a = k * math.pi / 4
            L((12 + 7 * math.cos(a), 12 + 7 * math.sin(a)), (12 + 9.5 * math.cos(a),
                                                             12 + 9.5 * math.sin(a)))
    elif name == "moon":
        pygame.draw.circle(d, col, (12 * s, 12 * s), 9 * s)
        pygame.draw.circle(d, (*col[:3], 0), (16 * s, 8.5 * s), 7.5 * s)
    elif name == "lock":
        R(4.5, 10.5, 15, 11, 2.5)
        pygame.draw.arc(d, col, (7.5 * s, 3 * s, 9 * s, 12 * s), 0, math.pi, lw)
        L((7.5, 9), (7.5, 10.5))
        L((16.5, 9), (16.5, 10.5))
    elif name == "plus":
        L((12, 5), (12, 19))
        L((5, 12), (19, 12))
    elif name == "dot":
        C(12, 12, 5, 0)
    elif name == "update":
        L((12, 3.5), (12, 15))
        L((7, 10), (12, 15), (17, 10))
        L((4, 20.5), (20, 20.5))
    elif name == "history":
        C(12, 12, 9)
        L((12, 7.5), (12, 12), (9, 14))
    elif name == "bell":
        pygame.draw.arc(d, col, (6.5 * s, 4.5 * s, 11 * s, 13 * s), 0, math.pi, lw)
        L((6.5, 11), (6.5, 17))
        L((17.5, 11), (17.5, 17))
        L((4.5, 17.5), (19.5, 17.5))
        L((10.5, 20.5), (13.5, 20.5))
    elif name == "keyboard":
        R(2.5, 6, 19, 12.5, 2.5)
        for x in (7, 10.3, 13.7, 17):
            C(x, 10.2, 1.1, 0)
        L((8, 14.5), (16, 14.5))
    elif name == "globe":
        C(12, 12, 9)
        L((3.5, 12), (20.5, 12))
        pygame.draw.ellipse(d, col, (8 * s, 3 * s, 8 * s, 18 * s), lw)
    elif name == "info":
        C(12, 12, 9)
        L((12, 11), (12, 16.5))
        C(12, 7.7, 1.3, 0)
    elif name == "sliders":
        L((3.5, 7.5), (20.5, 7.5))
        L((3.5, 16.5), (20.5, 16.5))
        C(9, 7.5, 2.8, 0)
        C(15, 16.5, 2.8, 0)
    else:   # unknown: a dot
        C(12, 12, 3, 0)


def icon(surf, name: str, center, size: int, color) -> None:
    def make():
        big = _clear_surface((size * SS, size * SS), color)
        _draw_icon(big, name, size * SS / 24, (*color[:3], 255), max(2, int(size * SS / 12)))
        return pygame.transform.smoothscale(big, (size, size))
    s = _icons.get_or((name, size, tuple(color[:3])), make)
    surf.blit(s, s.get_rect(center=center))


def circle(surf, center, radius: int, color) -> None:
    """Anti-aliased filled circle."""
    big = _clear_surface((radius * 2 * SS, radius * 2 * SS), color)
    pygame.draw.circle(big, (*color[:3], 255), (radius * SS, radius * SS), radius * SS)
    small = pygame.transform.smoothscale(big, (radius * 2, radius * 2))
    surf.blit(small, (center[0] - radius, center[1] - radius))


# -- composite widgets ---------------------------------------------------

def progress(surf, rect, theme, fraction: float, color=None, track=None) -> None:
    r = pygame.Rect(rect)
    rrect(surf, r, track or theme.c["track"], r.h // 2)
    w = int(r.w * max(0.0, min(1.0, fraction)))
    if w > 0:
        rrect(surf, (r.x, r.y, max(w, r.h), r.h), color or theme.c["accent"], r.h // 2)


def qr(surf, data: str, rect, dark, light) -> None:
    try:
        import segno
    except ImportError:
        return
    r = pygame.Rect(rect)
    code = segno.make(data, error="m")
    matrix = list(code.matrix)
    n = len(matrix) + 8   # quiet zone of 4 modules
    cell = max(1, min(r.w, r.h) // n)
    size = cell * n
    ox, oy = r.x + (r.w - size) // 2, r.y + (r.h - size) // 2
    pygame.draw.rect(surf, light, (ox, oy, size, size))
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                pygame.draw.rect(surf, dark, (ox + (x + 4) * cell, oy + (y + 4) * cell, cell, cell))


PNG_SIGNATURE = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])


def save_png(surf: pygame.Surface, path: str) -> None:
    """Write `surf` as a PNG (pygame's PNG support needs SDL2_image, which
    is not installed)."""
    w, h = surf.get_size()
    rgb = pygame.image.tobytes(surf, "RGB")
    row = w * 3
    raw = b"".join(bytes(1) + rgb[y * row:(y + 1) * row] for y in range(h))  # filter 0

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    with open(path, "wb") as f:
        f.write(PNG_SIGNATURE
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
