"""Widgets, screens and focus navigation.

Screens are drawn in "immediate mode": every frame, a screen draws itself
and returns its interactive widgets (each with a stable key). The app keeps
the focused key per screen, moves it with the arrow keys (spatial
navigation) and activates widgets with Enter, a click or a tap. The focus
is always visible."""

from __future__ import annotations

from dataclasses import dataclass, field

import pygame

from . import gfx


@dataclass
class Widget:
    key: str
    rect: pygame.Rect
    on_activate: object = None     # callable()
    enabled: bool = True
    focusable: bool = True
    radius: int = 0
    on_nav: object = None          # callable(action) -> bool: consume left/right...


# -- drawing helpers that also register widgets ----------------------------

class Canvas:
    """What a screen draws on: the surface, the theme and the widget list."""

    def __init__(self, app, surf: pygame.Surface):
        self.app = app
        self.surf = surf
        self.t = app.theme
        self.widgets: list[Widget] = []

    def s(self, v):
        return self.t.s(v)

    @property
    def c(self):
        return self.t.c

    def add(self, w: Widget) -> Widget:
        self.widgets.append(w)
        return w

    def focused(self, key: str) -> bool:
        return self.app.focus_key() == key

    def hovered(self, rect: pygame.Rect) -> bool:
        return self.app.inputs is not None and self.app.inputs.pointer_visible and \
            rect.collidepoint(self.app.inputs.pointer)

    # -- buttons -------------------------------------------------------------
    def button(self, key: str, rect, label: str, action=None, style: str = "primary",
               icon: str | None = None, enabled: bool = True, size: str = "md",
               sub: str | None = None) -> Widget:
        r = pygame.Rect(rect)
        c = self.c
        colors = {
            "primary": (c["accent"], c["on_accent"]),
            "success": (c["ok"], c["on_ok"]),
            "danger": (c["err"], c["on_err"]),
            "secondary": (c["surface"], c["text"]),
            "soft": (c["accent_soft"], c["accent"]),
            "ghost": (None, c["text"]),
            "danger_ghost": (None, c["err"]),
        }
        bg, fg = colors.get(style, colors["primary"])
        if not enabled:
            bg, fg = (c["track"] if bg is not None else None), c["text3"]
        radius = min(r.h // 2, self.s(22))
        if self.hovered(r) and enabled and bg is not None:
            bg = tuple(min(255, int(v * 1.06)) if sum(bg) < 382 else int(v * 0.96) for v in bg)
        if bg is not None:
            gfx.rrect(self.surf, r, bg, radius)
        elif self.hovered(r) and enabled:
            gfx.rrect(self.surf, r, c["surface2"], radius)
        if style == "secondary":
            gfx.rrect_border(self.surf, r, bg, c["border"], radius, self.s(2))
        cx = r.centerx
        tw = gfx.text_width(self.t, label, size, "semibold")
        isz = self.t.size(size)
        total = tw + (isz + self.s(14) if icon else 0)
        x0 = cx - total // 2
        if sub:
            cy = r.centery - self.s(19)
        else:
            cy = r.centery
        if icon:
            gfx.icon(self.surf, icon, (x0 + isz // 2, cy), isz, fg)
            x0 += isz + self.s(14)
        gfx.text(self.surf, self.t, label, (x0, cy), size, fg, "semibold", "midleft",
                 max_w=r.w - self.s(24))
        if sub:
            gfx.text(self.surf, self.t, sub, (cx, cy + self.s(46)), "xs",
                     fg if bg is not None and style not in ("secondary",) else c["text2"],
                     "medium", "center", max_w=r.w - self.s(24))
        return self.add(Widget(key, r, action, enabled, True, radius))

    def back(self, key: str, rect, action, label: str = "Back") -> Widget:
        """The "‹ Back" item of a page header; icon and label sit on the same
        columns as the settings categories."""
        r = pygame.Rect(rect)
        c = self.c
        if self.hovered(r):
            gfx.rrect(self.surf, r, c["surface2"], self.s(20))
        gfx.icon(self.surf, "back", (r.x + self.s(46), r.centery), self.s(38), c["text"])
        gfx.text(self.surf, self.t, label, (r.x + self.s(90), r.centery), "md", c["text"],
                 "medium", "midleft")
        return self.add(Widget(key, r, action, True, True, self.s(20)))

    def icon_button(self, key: str, center, icon: str, action=None, size: int = 64,
                    color=None) -> Widget:
        s = self.s(size)
        r = pygame.Rect(0, 0, s, s)
        r.center = center
        if self.hovered(r):
            gfx.rrect(self.surf, r, self.c["surface2"], s // 2)
        gfx.icon(self.surf, icon, center, int(s * 0.55), color or self.c["text2"])
        return self.add(Widget(key, r, action, True, True, s // 2))

    def toggle_switch(self, center_right, value: bool, enabled: bool = True) -> pygame.Rect:
        w, h = self.s(92), self.s(52)
        r = pygame.Rect(0, 0, w, h)
        r.midright = center_right
        c = self.c
        gfx.rrect(self.surf, r, (c["accent"] if value else c["track"]) if enabled else c["track"],
                  h // 2)
        knob = h - self.s(10)
        kx = r.right - knob // 2 - self.s(5) if value else r.x + knob // 2 + self.s(5)
        gfx.circle(self.surf, (kx, r.centery), knob // 2, (255, 255, 255))
        return r

    def row(self, key: str, rect, label: str, value: str = "", action=None, *,
            sub: str | None = None, toggle: bool | None = None, chevron: bool = True,
            enabled: bool = True, icon: str | None = None, value_color=None) -> Widget:
        """A settings row: label (and sub-label) on the left, value/switch on the right."""
        r = pygame.Rect(rect)
        c = self.c
        if self.hovered(r) and enabled:
            gfx.rrect(self.surf, r, c["surface2"], self.s(18))
        x = r.x + self.s(28)
        if icon:
            gfx.icon(self.surf, icon, (x + self.s(18), r.centery), self.s(36), c["text2"])
            x += self.s(58)
        tc = c["text"] if enabled else c["text3"]
        if sub:
            gfx.text(self.surf, self.t, label, (x, r.centery + self.s(2)), "md", tc, "medium",
                     "bottomleft", max_w=r.w // 2)
            gfx.text(self.surf, self.t, sub, (x, r.centery + self.s(2)), "xs", c["text2"],
                     "regular", "topleft", max_w=r.w - self.s(260))
        else:
            gfx.text(self.surf, self.t, label, (x, r.centery), "md", tc, "medium", "midleft",
                     max_w=r.w // 2)
        right = r.right - self.s(28)
        if toggle is not None:
            self.toggle_switch((right, r.centery), toggle, enabled)
        else:
            if chevron and action is not None:
                gfx.icon(self.surf, "chevron", (right - self.s(14), r.centery), self.s(30),
                         c["text3"])
                right -= self.s(48)
            if value:
                gfx.text(self.surf, self.t, value, (right, r.centery), "sm",
                         value_color or c["text2"], "regular", "midright",
                         max_w=r.w // 2 - self.s(40))
        return self.add(Widget(key, r, action, enabled, True, self.s(18)))

    def segmented(self, key: str, rect, options: list, value, on_change, enabled=True,
                  size: str = "sm") -> None:
        """options: [(value, label, sub or None)]; one focusable widget per option."""
        r = pygame.Rect(rect)
        c = self.c
        gfx.rrect(self.surf, r, c["surface2"], self.s(22))
        n = len(options)
        pad = self.s(6)
        w = (r.w - pad * (n + 1)) // n
        for i, (val, label, sub) in enumerate(options):
            orect = pygame.Rect(r.x + pad + i * (w + pad), r.y + pad, w, r.h - 2 * pad)
            sel = val == value
            if sel:
                gfx.card(self.surf, orect, self.t, self.s(18), c["surface"], lift=1)
            elif self.hovered(orect):
                gfx.rrect(self.surf, orect, c["track"], self.s(18))
            col = c["accent"] if sel else (c["text"] if enabled else c["text3"])
            if sub:
                gfx.text(self.surf, self.t, label, (orect.centerx, orect.centery + self.s(4)),
                         size, col, "semibold", "midbottom", max_w=orect.w - self.s(24))
                gfx.text(self.surf, self.t, sub, (orect.centerx, orect.centery + self.s(8)),
                         "xs", c["text2"], "regular", "midtop", max_w=orect.w - self.s(24))
            else:
                gfx.text(self.surf, self.t, label, orect.center, size, col, "semibold",
                         "center", max_w=orect.w - self.s(16))
            self.add(Widget(f"{key}:{val}", orect,
                            (lambda v=val: on_change(v)) if enabled else None, enabled, True,
                            self.s(18)))

    def chip(self, pos, label: str, color_key: str, anchor="topleft", dot=True) -> pygame.Rect:
        c = self.c
        soft = {"ok": "ok_soft", "err": "err_soft", "warn": "warn_soft",
                "accent": "accent_soft"}.get(color_key, "surface2")
        tw = gfx.text_width(self.t, label, "xs", "semibold")
        h = self.s(46)
        w = tw + self.s(36) + (self.s(26) if dot else 0)
        r = pygame.Rect(0, 0, w, h)
        setattr(r, anchor, pos)
        gfx.rrect(self.surf, r, c[soft], h // 2)
        x = r.x + self.s(18)
        if dot:
            gfx.circle(self.surf, (x + self.s(7), r.centery), self.s(7), c[color_key])
            x += self.s(26)
        gfx.text(self.surf, self.t, label, (x, r.centery), "xs", c[color_key], "semibold",
                 "midleft")
        return r


class Screen:
    """Base class of screens and dialogs."""
    modal = False            # dialogs are drawn over the previous screen
    default_focus: str | None = None
    name = "screen"

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:   # pragma: no cover
        raise NotImplementedError

    def on_nav(self, app, action: str) -> bool:
        """Screen-level handling of an action; True if consumed."""
        if action == "back":
            app.pop(self)
            return True
        return False

    def on_char(self, app, ch: str) -> bool:
        return False

    def on_state(self, app, state: dict) -> None:
        pass


def move_focus(widgets: list[Widget], current: Widget | None, action: str) -> Widget | None:
    """Spatial navigation: the nearest focusable widget in the direction."""
    cands = [w for w in widgets if w.focusable and w.enabled]
    if not cands:
        return None
    if current is None:
        return cands[0]
    if action == "next":
        i = next((k for k, w in enumerate(cands) if w.key == current.key), -1)
        return cands[(i + 1) % len(cands)]
    cx, cy = current.rect.center
    best, best_score = None, None
    for w in cands:
        if w.key == current.key:
            continue
        x, y = w.rect.center
        dx, dy = x - cx, y - cy
        if action == "right":
            primary, secondary, ok = dx, abs(dy), w.rect.left >= current.rect.centerx
        elif action == "left":
            primary, secondary, ok = -dx, abs(dy), w.rect.right <= current.rect.centerx
        elif action == "down":
            primary, secondary, ok = dy, abs(dx), w.rect.top >= current.rect.centery
        else:
            primary, secondary, ok = -dy, abs(dx), w.rect.bottom <= current.rect.centery
        if not ok or primary <= 0:
            continue
        score = primary + 2.5 * secondary
        if best_score is None or score < best_score:
            best, best_score = w, score
    return best
