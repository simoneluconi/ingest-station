"""Modal dialogs: confirmation, message, choice list, text input with an
on-screen keyboard (touch, arrows or a physical keyboard), share folder
browser."""

from __future__ import annotations

import pygame

from . import gfx
from .screens import wrap
from .ui import Canvas, Screen


def _panel(cv: Canvas, rect: pygame.Rect, w: int, h: int) -> pygame.Rect:
    overlay = pygame.Surface(rect.size, pygame.SRCALPHA)
    overlay.fill(cv.c["overlay"])
    cv.surf.blit(overlay, rect.topleft)
    box = pygame.Rect(0, 0, min(w, rect.w - cv.s(60)), min(h, rect.h - cv.s(60)))
    box.center = rect.center
    gfx.card(cv.surf, box, cv.t, cv.s(36))
    return box


class Confirm(Screen):
    modal = True
    default_focus = "no"

    def __init__(self, title, message, confirm_label, on_confirm, danger=True, cancel="Cancel"):
        self.title, self.message = title, message
        self.confirm_label, self.on_confirm = confirm_label, on_confirm
        self.danger, self.cancel = danger, cancel

    def draw(self, cv, rect):
        """Centred, like the message dialog: title, message, the two buttons."""
        app, s = cv.app, cv.s
        box = _panel(cv, rect, s(1100), s(500))
        cx = box.centerx
        gfx.text(cv.surf, cv.t, self.title, (cx, box.y + s(100)), "xl", cv.c["text"],
                 "semibold", "center", max_w=box.w - s(120), min_size="lg")
        wrap(cv, self.message, pygame.Rect(box.x + s(80), box.y + s(160), box.w - s(160),
                                           s(140)), "md", cv.c["text2"], 3, align="center")
        bw, bh, gap = s(400), s(110), s(24)
        by = box.bottom - bh - s(50)

        def yes():
            app.pop(self)
            self.on_confirm()
        cv.button("no", (cx - gap // 2 - bw, by, bw, bh), self.cancel,
                  lambda: app.pop(self), "secondary")
        cv.button("yes", (cx + gap // 2, by, bw, bh), self.confirm_label, yes,
                  "danger" if self.danger else "primary")


class Message(Screen):
    modal = True
    default_focus = "ok"

    def __init__(self, title, message, kind="info"):
        self.title, self.message, self.kind = title, message, kind

    def on_nav(self, app, action):
        if action in ("back", "ok"):
            app.pop(self)
            return True
        return False

    def draw(self, cv, rect):
        """Centred, like the result screen: icon in a soft circle (as on the
        tiles), title, message, OK."""
        s, c = cv.s, cv.c
        box = _panel(cv, rect, s(1000), s(560))
        tone = {"ok": "ok", "error": "err", "info": "accent"}[self.kind]
        icon = {"ok": "check", "error": "warning", "info": "dot"}[self.kind]
        cx = box.centerx
        gfx.circle(cv.surf, (cx, box.y + s(110)), s(52), c[f"{tone}_soft"])
        gfx.icon(cv.surf, icon, (cx, box.y + s(110)), s(60), c[tone])
        gfx.text(cv.surf, cv.t, self.title, (cx, box.y + s(210)), "xl", c["text"], "semibold",
                 "center", max_w=box.w - s(120), min_size="lg")
        msg = self.message[:1].upper() + self.message[1:]
        wrap(cv, msg, pygame.Rect(box.x + s(80), box.y + s(262), box.w - s(160), s(130)), "md",
             c["text2"], 3, align="center")
        cv.button("ok", (cx - s(170), box.bottom - s(140), s(340), s(100)), "OK",
                  lambda: cv.app.pop(self), "primary")


class Choice(Screen):
    """A list of options; `options` = [(value, label, sub or None)]."""
    modal = True

    def __init__(self, title, options, value, on_choose):
        self.title, self.options, self.value, self.on_choose = title, options, value, on_choose
        self.default_focus = f"opt:{value}"

    def draw(self, cv, rect):
        s, c = cv.s, cv.c
        rows = len(self.options)
        box = _panel(cv, rect, s(1000), s(200) + rows * s(112))
        gfx.text(cv.surf, cv.t, self.title, (box.x + s(56), box.y + s(48)), "xl", c["text"],
                 "semibold")
        for i, (val, label, sub) in enumerate(self.options):
            r = pygame.Rect(box.x + s(32), box.y + s(140) + i * s(112), box.w - s(64), s(100))
            sel = val == self.value
            if sel:
                gfx.rrect(cv.surf, r, c["accent_soft"], s(20))

            def pick(v=val):
                cv.app.pop(self)
                self.on_choose(v)
            cv.row(f"opt:{val}", r, label, "", pick, sub=sub, chevron=False,
                   icon="check" if sel else None)


OSK_LETTERS = ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm.-_"]
OSK_SYMBOLS = ["1234567890", "/\\:;()$&\"'", "!?#%*+=<>[", "{}|~^`,._-"]
OSK_NUMERIC = ["123", "456", "789", "0"]          # PIN and port: digits only


class TextInput(Screen):
    """Edit a value. Physical keyboards type directly (Enter confirms); the
    on-screen keyboard works with touch, mouse, arrows and a numeric keypad."""
    modal = True
    accepts_text = True
    default_focus = "k:0:0"

    def __init__(self, title, value, on_ok, password=False, numeric=False, hint=""):
        self.title, self.value, self.on_ok = title, value or "", on_ok
        self.password, self.numeric, self.hint = password, numeric, hint
        self.shift = False
        self.symbols = False
        self.show = not password

    def on_char(self, app, ch):
        self.value += ch
        return True

    def on_nav(self, app, action):
        if action == "backspace":
            self.value = self.value[:-1]
            return True
        if action == "back":
            app.pop(self)
            return True
        if action == "ok" and app.last_nav_code in (28, 96):   # Enter / keypad Enter
            self._done(app)
            return True
        return False

    def _done(self, app):
        app.pop(self)
        self.on_ok(self.value)

    def _key(self, ch):
        self.value += ch.upper() if self.shift and ch.isalpha() else ch
        if self.shift:
            self.shift = False

    def draw(self, cv, rect):
        app, s, c = cv.app, cv.s, cv.c
        rows = OSK_NUMERIC if self.numeric else (OSK_SYMBOLS if self.symbols else OSK_LETTERS)
        gap, kh = s(12), s(92)
        hint_h = s(56) if self.hint else s(0)
        # the panel fits its content: title, field, hint, keys, bottom row
        height = s(130 + 110 + 40) + hint_h + len(rows) * (kh + gap) + s(8) + kh + s(56)
        box = _panel(cv, rect, s(1500), height)
        x = box.x + s(56)
        gfx.text(cv.surf, cv.t, self.title, (x, box.y + s(74)), "xl", c["text"], "semibold",
                 "midleft", max_w=box.w - s(420))
        # touch has no Esc key: a way out without confirming
        cv.button("cancel", (box.right - s(56) - s(240), box.y + s(34), s(240), s(80)), "Cancel",
                  lambda: app.pop(self), "secondary", size="sm")
        field = pygame.Rect(x, box.y + s(130), box.w - s(112), s(110))
        gfx.rrect_border(cv.surf, field, c["surface2"], c["accent"], s(22), s(3))
        shown = self.value if self.show else "•" * len(self.value)
        tr = gfx.text(cv.surf, cv.t, shown or " ", (field.x + s(30), field.centery), "lg",
                      c["text"] if self.value else c["text3"], "medium", "midleft",
                      max_w=field.w - s(220))
        caret_x = tr.right + s(4) if self.value else field.x + s(30)
        pygame.draw.rect(cv.surf, c["accent"], (caret_x, field.centery - s(28), s(4), s(56)))
        if self.hint:
            gfx.text(cv.surf, cv.t, self.hint, (field.x + s(30), field.bottom + s(14)), "sm",
                     c["text3"], max_w=field.w - s(60))
        if self.password:
            cv.button("show", (field.right - s(180), field.y + s(15), s(160), s(80)),
                      "Hide" if self.show else "Show",
                      lambda: setattr(self, "show", not self.show), "ghost", size="sm")
        # keyboard
        ky = field.bottom + s(40) + hint_h
        kw_total = box.w - s(112)
        cols = max(len(r) for r in rows)
        kw = (kw_total - gap * (cols - 1)) // cols
        for ri, row in enumerate(rows):
            off = (kw_total - (len(row) * kw + (len(row) - 1) * gap)) // 2
            for ci, ch in enumerate(row):
                label = ch.upper() if (self.shift and ch.isalpha()) else ch
                r = pygame.Rect(x + off + ci * (kw + gap), ky + ri * (kh + gap), kw, kh)
                cv.button(f"k:{ri}:{ci}", r, label, lambda ch=ch: self._key(ch), "secondary",
                          size="lg")
        by = ky + len(rows) * (kh + gap) + s(8)
        bw = (kw_total - 4 * gap) // 5
        specials = []
        if not self.numeric:
            specials += [("shift", "Shift" if not self.shift else "SHIFT",
                          lambda: setattr(self, "shift", not self.shift)),
                         ("sym", "ABC" if self.symbols else "#+=",
                          lambda: setattr(self, "symbols", not self.symbols)),
                         ("space", "Space", lambda: self._key(" "))]
        specials += [("del", "Delete", lambda: setattr(self, "value", self.value[:-1]))]
        for i, (k, label, fn) in enumerate(specials):
            cv.button(k, (x + i * (bw + gap), by, bw, kh), label, fn, "secondary", size="md")
        cv.button("done", (x + 4 * (bw + gap), by, bw, kh), "Done", lambda: self._done(app),
                  "primary", size="md")


class FolderBrowser(Screen):
    """Browse the folders of the configured share and pick one."""
    modal = True

    def __init__(self, start: str, on_pick):
        self.path = start.strip("/")
        self.on_pick = on_pick
        self.items: list[str] | None = None
        self.error = ""
        self.offset = 0
        self.default_focus = "select"

    def load(self, app):
        self.items, self.error, self.offset = None, "", 0

        def done(ok, res):
            if ok:
                self.items = res
            else:
                self.items, self.error = [], str(res)
        app.command("share.list", done, path=self.path)

    def on_nav(self, app, action):
        if action == "back":
            if self.path:
                self.path = "/".join(self.path.split("/")[:-1])
                self.load(app)
            else:
                app.pop(self)
            return True
        if action in ("pagedown", "pageup"):
            self.offset = max(0, self.offset + (6 if action == "pagedown" else -6))
            return True
        return False

    def draw(self, cv, rect):
        app, s, c = cv.app, cv.s, cv.c
        if self.items is None and not self.error and not getattr(self, "_loading", False):
            self._loading = True
            self.load(app)
        box = _panel(cv, rect, s(1500), s(960))
        x = box.x + s(56)
        gfx.text(cv.surf, cv.t, "Destination folder", (x, box.y + s(44)), "xl", c["text"],
                 "semibold")
        crumbs = "Share root" + ("  ›  " + "  ›  ".join(self.path.split("/")) if self.path else "")
        gfx.text(cv.surf, cv.t, crumbs, (x, box.y + s(124)), "md", c["accent"], "medium",
                 max_w=box.w - s(112))
        lst = pygame.Rect(x, box.y + s(190), box.w - s(112), s(530))
        gfx.rrect(cv.surf, lst, c["surface2"], s(24))
        if self.items is None:
            gfx.text(cv.surf, cv.t, "Loading…", lst.center, "md", c["text2"], anchor="center")
        elif self.error:
            wrap(cv, self.error, lst.inflate(-s(60), -s(60)), "md", c["err"], 4)
        else:
            entries = ([("..", "Up one level")] if self.path else []) + \
                [(n, "") for n in self.items]
            if not entries:
                gfx.text(cv.surf, cv.t, "No sub-folders here", lst.center, "md", c["text2"],
                         anchor="center")
            per = 6
            self.offset = min(self.offset, max(0, len(entries) - per))
            for i, (name, sub) in enumerate(entries[self.offset:self.offset + per]):
                r = pygame.Rect(lst.x + s(12), lst.y + s(12) + i * s(90), lst.w - s(24), s(84))

                def open_(n=name):
                    if n == "..":
                        self.path = "/".join(self.path.split("/")[:-1])
                    else:
                        self.path = f"{self.path}/{n}".strip("/")
                    self.load(app)
                cv.row(f"dir:{name}", r, name if name != ".." else "..", sub, open_,
                       icon="back" if name == ".." else "folder")
            if len(entries) > per:
                gfx.text(cv.surf, cv.t, f"{self.offset + 1}-{min(len(entries), self.offset + per)}"
                         f" of {len(entries)} · Page Up/Down", (lst.right - s(20), lst.bottom + s(12)),
                         "xs", c["text3"], anchor="topright")
        by = box.bottom - s(150)

        def pick():
            app.pop(self)
            self.on_pick(self.path)

        def new_folder():
            def made(name):
                name = name.strip().strip("/")
                if not name:
                    return
                target = f"{self.path}/{name}".strip("/")
                app.command("share.mkdir", lambda ok, r: self.load(app), path=target)
            app.push(TextInput("New folder name", "", made))
        cv.button("select", (box.right - s(56) - s(460), by, s(460), s(100)),
                  "Use this folder", pick, "primary", icon="check")
        cv.button("mkdir", (x, by, s(360), s(100)), "New folder", new_folder, "secondary",
                  icon="plus")
        cv.button("cancel", (x + s(390), by, s(300), s(100)), "Cancel", lambda: app.pop(self),
                  "secondary")
