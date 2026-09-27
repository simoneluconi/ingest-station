"""Colours (light and dark), type scale and sizes. Everything is designed
for 1920x1080 and scaled to the actual screen; the smallest text is large
enough to be read from two metres."""

from __future__ import annotations

import os

FONTS_DIR = "/opt/ingest/assets/fonts"
WEIGHTS = {"regular": "Inter-Regular.ttf", "medium": "Inter-Medium.ttf",
           "semibold": "Inter-SemiBold.ttf", "bold": "Inter-Bold.ttf"}

# type scale at 1080p (px)
SIZES = {"xs": 26, "sm": 30, "md": 36, "lg": 44, "xl": 58, "xxl": 84, "hero": 132}

PALETTES = {
    "light": {
        "bg": (241, 244, 249), "surface": (255, 255, 255), "surface2": (246, 248, 252),
        "border": (223, 229, 238), "text": (17, 24, 39), "text2": (91, 104, 125),
        "text3": (148, 160, 178), "accent": (79, 70, 229), "accent2": (99, 91, 255),
        "on_accent": (255, 255, 255), "accent_soft": (236, 238, 255),
        "ok": (22, 163, 74), "ok_soft": (220, 252, 231), "on_ok": (255, 255, 255),
        "err": (220, 38, 38), "err_soft": (254, 226, 226), "on_err": (255, 255, 255),
        "warn": (217, 119, 6), "warn_soft": (254, 243, 199), "on_warn": (255, 255, 255),
        "track": (228, 233, 241), "shadow": (17, 24, 39), "shadow_alpha": 26,
        "focus": (79, 70, 229), "overlay": (15, 23, 42, 110),
    },
    "dark": {
        "bg": (12, 17, 28), "surface": (22, 29, 44), "surface2": (29, 37, 55),
        "border": (43, 53, 76), "text": (236, 240, 247), "text2": (160, 172, 192),
        "text3": (105, 118, 140), "accent": (129, 140, 248), "accent2": (150, 160, 255),
        "on_accent": (12, 17, 28), "accent_soft": (35, 40, 78),
        "ok": (52, 211, 153), "ok_soft": (17, 56, 45), "on_ok": (8, 30, 22),
        "err": (248, 113, 113), "err_soft": (69, 26, 30), "on_err": (40, 8, 8),
        "warn": (251, 191, 36), "warn_soft": (66, 48, 12), "on_warn": (40, 26, 2),
        "track": (43, 53, 76), "shadow": (0, 0, 0), "shadow_alpha": 70,
        "focus": (165, 180, 252), "overlay": (0, 0, 0, 150),
    },
}

# colour of each source status
STATUS_COLOR = {"empty": "text3", "detected": "accent", "mounting": "accent",
                "scanning": "accent", "ready": "accent", "countdown": "warn",
                "queued": "text2", "importing": "accent", "done": "ok", "error": "err",
                "unsupported": "warn", "update": "accent", "installer": "text2",
                "ejected": "ok"}


class Theme:
    def __init__(self, name: str, width: int, height: int):
        self.name = name if name in PALETTES else "light"
        self.c = PALETTES[self.name]
        self.width, self.height = width, height
        self.scale = min(width / 1920, height / 1080)

    def s(self, v: float) -> int:
        """Scale a 1080p length."""
        return max(1, int(round(v * self.scale)))

    def size(self, name: str) -> int:
        return self.s(SIZES.get(name, name if isinstance(name, int) else 36))

    @staticmethod
    def font_path(weight: str) -> str:
        return os.path.join(FONTS_DIR, WEIGHTS.get(weight, WEIGHTS["regular"]))
