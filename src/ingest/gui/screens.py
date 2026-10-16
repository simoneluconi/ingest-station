"""Main screens: home (one tile per slot/device), source summary, import
progress, pause (share lost) and final result."""

from __future__ import annotations

import datetime as dt
import math
import time

import pygame

from . import gfx
from .theme import STATUS_COLOR
from .ui import Canvas, Screen


def human(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1000
    return str(n)


def duration(sec) -> str:
    if sec is None:
        return "–"
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60} min {sec % 60:02d} s" if sec < 600 else f"{sec // 60} min"
    return f"{sec // 3600} h {sec // 60 % 60:02d} min"


def when(ts) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if d.date() == today:
        return f"today {d:%H:%M}"
    if (today - d.date()).days == 1:
        return f"yesterday {d:%H:%M}"
    if d.year == today.year:
        return f"{d.day} {d:%b %H:%M}"
    return f"{d.day} {d:%b %Y %H:%M}"


def source_icon(src: dict) -> str:
    if src.get("kind") == "camera":
        return "camera"
    name = (src.get("name", "") + " " + src.get("model", "")).upper()
    if any(k in name for k in ("SSD", "HDD", "DISK", "PORTABLE", "EXPANSION", "PASSPORT")) \
            and src.get("capacity", 0) > 300e9:
        return "drive"
    if src.get("capacity", 0) and src.get("slot", "").endswith((":1", ":2", ":3")) or \
            any(k in name for k in ("SD", "CF", "READER", "MMC", "CARD")):
        return "card"
    return "usb"


STATUS_LABEL = {"empty": "No card", "detected": "Detected", "mounting": "Opening…",
                "scanning": "Scanning…", "ready": "Ready", "countdown": "Auto-import",
                "queued": "Queued", "importing": "Importing", "done": "Done",
                "error": "Error", "unsupported": "Not supported", "update": "Update",
                "installer": "Installer stick", "ejected": "Ejected"}
# titles of the outcome block on a tile
STATUS_TITLE = {"error": "Cannot read this device", "unsupported": "Not supported",
                "update": "Update package found", "installer": "Installation stick"}


def link_speed(src: dict) -> tuple[str, bool]:
    """("5 Gbps", slow): the negotiated USB speed, and whether a USB 3 device
    in a USB 3 port runs on a slower link (USB 2 cable, loose plug)."""
    speed = src.get("usb_speed") or 0
    if not speed:
        return "", False
    label = f"{speed / 1000:g} Gbps" if speed >= 1000 else f"{speed:g} Mbps"
    return label, bool(src.get("usb_slow"))


def draw_meta(cv: Canvas, src: dict, pos, max_w: int) -> None:
    """"LABEL · EXFAT · 124 GB · 5 Gbps"; a slow USB link in amber."""
    t, c, s = cv.t, cv.c, cv.s
    parts = [src.get("label"),
             (src.get("fs") or "").upper() if src.get("fs") not in ("", None, "camera") else "",
             human(src["capacity"]) if src.get("capacity") else ""]
    meta = " · ".join(x for x in parts if x)
    speed, slow = link_speed(src)
    x, y = pos
    if meta:
        r = gfx.text(cv.surf, t, meta + (" · " if speed else ""), (x, y), "sm", c["text2"],
                     max_w=max_w)
        x = r.right
    if speed:
        gfx.text(cv.surf, t, f"{speed} · slow link" if slow else speed, (x, y), "sm",
                 c["warn"] if slow else c["text2"], "semibold" if slow else "regular",
                 max_w=max(0, pos[0] + max_w - x))


def eta_for(state: dict, nbytes: int, verify: str) -> float:
    st = state.get("settings", {}).get("stats", {})
    copy = max(1.0, st.get("copy_mbps", 60)) * 1e6
    ver = max(1.0, st.get("verify_mbps", 90)) * 1e6
    t = nbytes / copy
    if verify == "full":
        t += nbytes / ver
    return t


class Home(Screen):
    name = "home"

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        st = app.state or {}
        sources = [x for x in st.get("sources", [])]
        pad = s(56)
        area = rect.inflate(-2 * pad, -2 * s(36))
        # bottom action bar
        bar_h = s(96)
        bar = pygame.Rect(area.x, area.bottom - bar_h, area.w, bar_h)
        cv.button("settings", (bar.right - s(300), bar.y, s(300), bar_h), "Settings",
                  lambda: app.open_settings(), "secondary", icon="gear")
        cv.button("history", (bar.right - s(620), bar.y, s(300), bar_h), "History",
                  lambda: app.open_history(), "secondary", icon="history")
        q = st.get("queue", [])
        hint = "Arrow keys to move · Enter to open"
        if q:
            hint = f"{len(q)} waiting in the queue"
        gfx.text(cv.surf, t, hint, (bar.x, bar.centery), "sm", c["text3"], "regular",
                 "midleft")
        grid = pygame.Rect(area.x, area.y, area.w, area.h - bar_h - s(36))
        if not sources:
            self._empty(cv, grid, st)
            return
        n = len(sources)
        cols = 1 if n == 1 else 2 if n <= 4 else 3
        rows = math.ceil(n / cols)
        gap = s(36)
        tw = (grid.w - gap * (cols - 1)) // cols
        th = min((grid.h - gap * (rows - 1)) // rows, s(500) if n == 1 else s(520))
        if n == 1:
            tw = min(tw, s(1400))
        left = grid.x + (grid.w - (tw * cols + gap * (cols - 1))) // 2
        top = grid.y + (grid.h - (th * rows + gap * (rows - 1))) // 2
        for i, src in enumerate(sources):
            r = pygame.Rect(left + (i % cols) * (tw + gap), top + (i // cols) * (th + gap),
                            tw, th)
            self._tile(cv, r, src, st, big=(n == 1))

    def _empty(self, cv: Canvas, rect: pygame.Rect, st: dict) -> None:
        t, c, s = cv.t, cv.c, cv.s
        box = pygame.Rect(0, 0, min(rect.w, s(1100)), min(rect.h, s(620)))
        box.center = rect.center
        gfx.card(cv.surf, box, t, s(40))
        cx = box.centerx
        circle_r = s(92)
        gfx.circle(cv.surf, (cx, box.y + s(170)), circle_r, c["accent_soft"])
        gfx.icon(cv.surf, "card", (cx - s(34), box.y + s(170)), s(84), c["accent"])
        gfx.icon(cv.surf, "camera", (cx + s(40), box.y + s(182)), s(64), c["accent"])
        gfx.text(cv.surf, t, "Insert a card or connect a camera", (cx, box.y + s(330)), "xl",
                 c["text"], "semibold", "center")
        share = st.get("share", {})
        if share.get("state") == "online":
            msg = "Files will be copied to " + self_share_name(share)
            col = c["text2"]
        elif share.get("state") == "unconfigured":
            msg = "No destination yet: open Settings to connect a network share"
            col = c["warn"]
        else:
            msg = "The network share is offline: " + (share.get("reason") or "checking…")
            col = c["err"]
        gfx.text(cv.surf, t, msg, (cx, box.y + s(420)), "md", col, "regular", "center",
                 max_w=box.w - s(80))
        gfx.text(cv.surf, t, "Card readers, USB drives, cameras and phones are detected "
                 "automatically", (cx, box.y + s(490)), "sm", c["text3"], "regular", "center",
                 max_w=box.w - s(80))

    def _tile(self, cv: Canvas, r: pygame.Rect, src: dict, st: dict, big: bool) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        status = src["status"]
        color_key = STATUS_COLOR.get(status, "accent")
        key = f"tile:{src['id']}"
        gfx.card(cv.surf, r, t, s(32))
        if cv.hovered(r):
            gfx.rrect_border(cv.surf, r, c["surface"], c["border"], s(32), s(2))
        inner = r.inflate(-2 * s(40), -2 * s(36))
        # header: icon badge, name, chip
        badge = pygame.Rect(inner.x, inner.y, s(88), s(88))
        gfx.rrect(cv.surf, badge, c["accent_soft"] if status != "empty" else c["surface2"],
                  s(24))
        gfx.icon(cv.surf, source_icon(src), badge.center, s(50),
                 c["accent"] if status != "empty" else c["text3"])
        tx = badge.right + s(28)
        chip = cv.chip((inner.right, inner.y + s(4)), STATUS_LABEL.get(status, status),
                       color_key, anchor="topright")
        gfx.text(cv.surf, t, src.get("name") or "Device", (tx, inner.y + s(2)), "lg",
                 c["text"] if status != "empty" else c["text2"], "semibold",
                 max_w=chip.x - tx - s(20), min_size="md")
        draw_meta(cv, src, (tx, inner.y + s(58)), inner.right - tx)
        body = pygame.Rect(inner.x, badge.bottom + s(28), inner.w, inner.bottom - badge.bottom - s(28))
        self._tile_body(cv, body, src, st, big)
        app_act = (lambda sid=src["id"]: app.open_source(sid))
        cv.add(_widget(key, r, app_act if status != "empty" else None, s(32)))

    def _tile_body(self, cv: Canvas, b: pygame.Rect, src: dict, st: dict, big: bool) -> None:
        t, c, s = cv.t, cv.c, cv.s
        status = src["status"]
        sm = src.get("summary") or {}
        hero = "hero" if big else "xxl"
        if status == "empty":
            gfx.text(cv.surf, t, "Insert a card in this slot", b.midleft, "md", c["text3"],
                     anchor="midleft")
        elif status in ("detected", "mounting", "scanning"):
            spinner(cv, (b.x + s(30), b.centery), s(24), c["accent"])
            gfx.text(cv.surf, t, "Reading the files…", (b.x + s(80), b.centery), "md",
                     c["text2"], anchor="midleft")
        elif status in ("ready", "countdown", "queued") and sm:
            if sm["new_files"]:
                y = b.y + (b.h - t.size(hero) - s(70)) // 2
                r = gfx.text(cv.surf, t, f"{sm['new_files']}", (b.x, y), hero, c["text"], "bold")
                gfx.text(cv.surf, t, "new files" if sm["new_files"] != 1 else "new file",
                         (r.right + s(18), gfx.baseline(t, r, hero, "bold")), "lg", c["text2"],
                         "medium", "baseline")
                sub = f"{human(sm['new_bytes'])} to copy"
                if sm["known_files"]:
                    sub += f" · {sm['known_files']} already imported"
                gfx.text(cv.surf, t, sub, (b.x, r.bottom + s(16)), "md", c["text2"],
                         max_w=b.w, min_size="sm")
            else:
                last = (sm.get("previous") or [{}])[-1]
                self._status_block(cv, b, "ok", "check", "Everything already imported",
                                   f"{sm['files']} files · {human(sm['bytes'])}",
                                   f"Last import {when(last['date'])}" if last.get("date")
                                   else "")
            if status == "countdown" and src.get("countdown"):
                gfx.text(cv.surf, t, f"Import starts in {src['countdown']} s",
                         (b.x, b.bottom), "md", c["warn"], "semibold", "bottomleft")
            elif status == "queued":
                q = st.get("queue", [])
                pos = q.index(src["id"]) + 1 if src["id"] in q else 1
                gfx.text(cv.surf, t, f"Waiting in the queue (#{pos})", (b.x, b.bottom), "md",
                         c["text2"], "medium", "bottomleft")
        elif status == "importing":
            a = st.get("active") or {}
            pct = a.get("percent", 0.0)
            y = b.y + (b.h - t.size(hero) - s(90)) // 2
            r = gfx.text(cv.surf, t, f"{int(pct)}%", (b.x, y), hero, c["accent"], "bold")
            phase = {"copy": "copying", "verify": "verifying", "finishing": "finishing",
                     "starting": "starting"}.get(a.get("phase", ""), a.get("phase", ""))
            gfx.text(cv.surf, t, phase, (r.right + s(18), gfx.baseline(t, r, hero, "bold")), "lg",
                     c["text2"], "medium", "baseline")
            gfx.progress(cv.surf, (b.x, r.bottom + s(20), b.w, s(18)), t, pct / 100)
            if a.get("paused"):
                info = "Paused: " + (a.get("error") or "")
                col = c["err"]
            else:
                info = f"{human(a.get('speed_now'))}/s · {duration(a.get('eta'))} left"
                col = c["text2"]
            gfx.text(cv.surf, t, info, (b.x, r.bottom + s(58)), "md", col, max_w=b.w)
        elif status == "done":
            res = src.get("result") or {}
            self._status_block(cv, b, "ok", "check", "Import complete",
                               f"{res.get('files', 0)} files · {human(res.get('bytes'))} → "
                               f"{res.get('folder', '')}" if res else "",
                               src.get("message") or "")
        elif status == "ejected":
            self._status_block(cv, b, "ok", "check", "Safe to remove", "",
                               src.get("message") or "")
        else:   # error, unsupported, update, installer
            tone = {"error": "err", "unsupported": "warn"}.get(status, "accent")
            self._status_block(cv, b, tone,
                               "warning" if status in ("error", "unsupported") else "update",
                               STATUS_TITLE.get(status, STATUS_LABEL.get(status, status)),
                               src.get("message") or "")

    def _status_block(self, cv: Canvas, b: pygame.Rect, tone: str, icon: str, title: str,
                      line1: str = "", line2: str = "") -> None:
        status_block(cv, b, tone, icon, title, line1, line2)


def status_block(cv: Canvas, b: pygame.Rect, tone: str, icon: str, title: str,
                 line1: str = "", line2: str = "") -> None:
    """The one layout for an outcome (a tile's done, nothing new, ejected,
    errors; the wizard's connection test): icon in a soft circle, title in
    the tone's colour, a detail line (up to 2 lines) and a quieter hint
    line. Vertically centred in `b`."""
    t, c, s = cv.t, cv.c, cv.s
    x, w = b.x + s(96), b.w - s(100)
    cy = b.y + b.h // 2 - s(30)
    gfx.circle(cv.surf, (b.x + s(36), cy), s(36), c[f"{tone}_soft"])
    gfx.icon(cv.surf, icon, (b.x + s(36), cy), s(44), c[tone])
    gfx.text(cv.surf, t, title, (x, cy), "lg", c[tone], "semibold", "midleft",
             max_w=w, min_size="md")
    y = cy + s(30)
    if line1:
        y += wrap(cv, line1, pygame.Rect(x, y, w, s(100)), "md", c["text2"], 2) + s(4)
    if line2:
        gfx.text(cv.surf, t, line2, (x, y), "sm", c["text3"], max_w=w, min_size="xs")


def self_share_name(share: dict) -> str:
    path = share.get("path", "").strip("/")
    sub = share.get("subdir", "").strip("/")
    return f"{share.get('server', '')} › {path}" + (f" › {sub}" if sub else "")


def _widget(key, rect, action, radius):
    from .ui import Widget
    return Widget(key, pygame.Rect(rect), action, action is not None, action is not None, radius)


def spinner(cv: Canvas, center, radius: int, color) -> None:
    a = (time.monotonic() * 5) % (2 * math.pi)
    for k in range(8):
        ang = a + k * math.pi / 4
        alpha = int(255 * (k + 1) / 8)
        col = tuple(int(bg + (fg - bg) * alpha / 255) for fg, bg in zip(color, cv.c["surface"]))
        x = center[0] + int(math.cos(ang) * radius)
        y = center[1] + int(math.sin(ang) * radius)
        pygame.draw.circle(cv.surf, col, (x, y), max(2, radius // 4))
    cv.app.animating = True


def wrap(cv: Canvas, s: str, rect: pygame.Rect, size: str, color, max_lines: int = 3,
         weight: str = "regular", align: str = "left") -> int:
    """Draw word-wrapped text; returns the height used."""
    words = s.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if gfx.text_width(cv.t, cand, size, weight) <= rect.w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = gfx.fit(cv.t, lines[-1] + "…", size, weight, rect.w)
    lh = gfx.line_height(cv.t, size)
    for i, ln in enumerate(lines):
        if align == "center":
            gfx.text(cv.surf, cv.t, ln, (rect.centerx, rect.y + i * lh), size, color, weight,
                     "midtop")
        else:
            gfx.text(cv.surf, cv.t, ln, (rect.x, rect.y + i * lh), size, color, weight)
    return len(lines) * lh


def destination_preview(app, share: dict, src: dict) -> str:
    """Where the files will go, with the name of the folder to be created."""
    if share.get("state") == "unconfigured" or not share.get("server"):
        return "No destination configured: open Settings"
    try:
        from ingest.daemon import naming
        sm = src.get("summary") or {}
        label = naming.source_label(src.get("label"), src.get("model"),
                                    hint=sm.get("brand", ""))
        folder = naming.folder_name(dt.datetime.now(), label,
                                    app.settings.get("folder_schema", naming.DEFAULT_SCHEMA))
    except Exception:  # noqa: BLE001 - preview only
        folder = "…"
    parts = [share.get("server", ""), share.get("path", "").strip("/")]
    if share.get("subdir"):
        parts.append(share["subdir"].strip("/"))
    return " › ".join(p for p in parts if p) + f" › {folder}"


class SourceScreen(Screen):
    """Summary of a source: what is new, what was already imported, and the
    choices (import new / import all / eject, verification)."""
    name = "source"
    default_focus = "import_new"

    def __init__(self, source_id: str, verify: str):
        self.sid = source_id
        self.verify = verify

    def source(self, app) -> dict | None:
        return next((x for x in (app.state or {}).get("sources", []) if x["id"] == self.sid),
                    None)

    def on_state(self, app, state) -> None:
        if self.source(app) is None:
            app.pop(self)

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        src = self.source(app)
        if src is None:
            return
        st = app.state or {}
        pad = s(56)
        area = rect.inflate(-2 * pad, -2 * s(36))
        cv.back("back", (area.x, area.y, s(220), s(88)), lambda: app.pop(self))
        # title: badge, name and details centred on the "Back" line
        badge = pygame.Rect(area.x + s(270), area.y, s(88), s(88))
        gfx.rrect(cv.surf, badge, c["accent_soft"], s(24))
        gfx.icon(cv.surf, source_icon(src), badge.center, s(50), c["accent"])
        gfx.text(cv.surf, t, src.get("name") or "Device", (badge.right + s(26), badge.centery
                                                           + s(6)),
                 "xl", c["text"], "semibold", "bottomleft", max_w=area.w - s(800))
        draw_meta(cv, src, (badge.right + s(28), badge.centery + s(6)), area.w - s(800))
        cv.chip((area.right, badge.centery), STATUS_LABEL.get(src["status"], src["status"]),
                STATUS_COLOR.get(src["status"], "accent"), anchor="midright")
        body = pygame.Rect(area.x, area.y + s(130), area.w, area.h - s(130))
        sm = src.get("summary")
        status = src["status"]
        if status in ("detected", "mounting", "scanning") or not sm:
            if status in ("error", "unsupported"):
                self._problem(cv, body, src)
            else:
                box = pygame.Rect(body.x, body.y, body.w, s(300))
                gfx.card(cv.surf, box, t, s(32))
                spinner(cv, (box.x + s(90), box.centery), s(30), c["accent"])
                gfx.text(cv.surf, t, "Reading the files and comparing them with what was "
                         "already imported…", (box.x + s(160), box.centery), "md", c["text2"],
                         anchor="midleft", max_w=box.w - s(200))
            return
        self._summary(cv, body, src, st, sm)

    def _problem(self, cv, body, src) -> None:
        t, c, s = cv.t, cv.c, cv.s
        app = cv.app
        box = pygame.Rect(body.x, body.y, body.w, s(330))
        gfx.card(cv.surf, box, t, s(32), c["err_soft"] if src["status"] == "error"
                 else c["warn_soft"], lift=0)
        col = c["err"] if src["status"] == "error" else c["warn"]
        gfx.icon(cv.surf, "warning", (box.x + s(90), box.y + s(90)), s(72), col)
        wrap(cv, src.get("message") or "Error", pygame.Rect(box.x + s(170), box.y + s(56),
                                                            box.w - s(220), s(200)), "lg", col, 3,
             "medium")
        y = box.bottom + s(40)
        cv.button("rescan", (body.x, y, s(360), s(100)), "Scan again",
                  lambda: app.command("rescan", source=self.sid), "secondary", icon="refresh")
        cv.button("eject", (body.x + s(390), y, s(300), s(100)), "Eject",
                  lambda: app.eject(self.sid), "ghost", icon="eject")

    def _summary(self, cv, body, src, st, sm) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        share = st.get("share", {})
        share_ok = share.get("state") == "online"
        status = src["status"]
        gap = s(40)
        bh = s(124)
        ay = body.bottom - bh
        cards_h = ay - body.y - s(48)
        left = pygame.Rect(body.x, body.y, int(body.w * 0.44), cards_h)
        right = pygame.Rect(left.right + gap, body.y, body.right - left.right - gap, cards_h)
        # -- left card: what is new and what was imported before, or the outcome
        gfx.card(cv.surf, left, t, s(32))
        if status == "done" and src.get("result"):
            # the summary is the one from before the import: show the outcome
            self._done_card(cv, left, src)
        else:
            self._new_card(cv, left, sm)
        self._right_card(cv, right, src, st, sm, share, share_ok)
        self._actions(cv, body, ay, bh, src, sm, share, share_ok)

    def _done_card(self, cv, left, src) -> None:
        t, c, s = cv.t, cv.c, cv.s
        x, y = left.x + s(52), left.y + s(48)
        res = src["result"]
        gfx.text(cv.surf, t, "LAST IMPORT", (x, y), "xs", c["text3"], "semibold")
        status_block(cv, pygame.Rect(x, y + s(40), left.w - s(104), left.h - s(240)), "ok",
                     "check", "Import complete",
                     f"{res.get('files', 0)} files · {human(res.get('bytes'))} → "
                     f"{res.get('folder', '')}", src.get("message") or "")
        wrap(cv, "The device was released. To import from it again, scan it first.",
             pygame.Rect(x, left.bottom - s(150), left.w - s(104), s(100)), "sm", c["text3"], 2)

    def _new_card(self, cv, left, sm) -> None:
        t, c, s = cv.t, cv.c, cv.s
        x, y = left.x + s(52), left.y + s(48)
        gfx.text(cv.surf, t, "NEW", (x, y), "xs", c["text3"], "semibold")
        r = gfx.text(cv.surf, t, str(sm["new_files"]), (x, y + s(36)), "hero",
                     c["accent"] if sm["new_files"] else c["ok"], "bold")
        gfx.text(cv.surf, t, "files" if sm["new_files"] != 1 else "file",
                 (r.right + s(20), gfx.baseline(t, r, "hero", "bold")), "xl", c["text2"],
                 "medium", "baseline")
        sub_y = r.bottom + s(12)
        gfx.text(cv.surf, t, f"{human(sm['new_bytes'])} to copy" if sm["new_files"]
                 else "Nothing new on this device", (x, sub_y), "lg", c["text2"])
        div_y = sub_y + s(96)
        pygame.draw.line(cv.surf, c["border"], (x, div_y), (left.right - s(52), div_y), s(2))
        yy = div_y + s(40)
        if sm["known_files"]:
            gfx.text(cv.surf, t, f"{sm['known_files']} already imported "
                     f"({human(sm['known_bytes'])})", (x, yy), "md", c["text"], "semibold")
            yy += s(62)
            for p in sm.get("previous", [])[:2]:
                used = wrap(cv, f"{p['files']} files · {when(p['date'])} → {p['folder']}",
                            pygame.Rect(x, yy, left.w - s(104), s(120)), "sm", c["text2"], 2)
                yy += used + s(18)
        else:
            wrap(cv, "Nothing from this device was imported before",
                 pygame.Rect(x, yy, left.w - s(104), s(120)), "md", c["text2"], 2)

    def _right_card(self, cv, right, src, st, sm, share, share_ok) -> None:
        """Verification and destination."""
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        gfx.card(cv.surf, right, t, s(32))
        rx, ry, rw = right.x + s(52), right.y + s(48), right.w - s(104)
        gfx.text(cv.surf, t, "VERIFICATION", (rx, ry), "xs", c["text3"], "semibold")
        opts = []
        for val, label, desc in (("none", "None", "fastest"),
                                 ("hash", "Hash", "recommended"),
                                 ("full", "Full", "safest")):
            opts.append((val, label, f"≈ {duration(eta_for(st, sm['new_bytes'], val))}"
                         if sm["new_bytes"] else desc))
        cv.segmented("verify", (rx, ry + s(56), rw, s(150)), opts, self.verify,
                     self._set_verify, size="md")
        desc = {"none": "Fastest. Files are copied without checksums.",
                "hash": "Recommended. A checksum of every file is computed while copying and "
                        "saved next to the copy.",
                "full": "Safest. After the copy, every file is read back from the share and "
                        "compared."}[self.verify]
        wrap(cv, desc, pygame.Rect(rx, ry + s(240), rw, s(110)), "sm", c["text2"], 2)
        dy = right.bottom - s(200)
        pygame.draw.line(cv.surf, c["border"], (rx, dy), (rx + rw, dy), s(2))
        gfx.text(cv.surf, t, "DESTINATION", (rx, dy + s(34)), "xs", c["text3"], "semibold")
        wrap(cv, destination_preview(app, share, src), pygame.Rect(rx, dy + s(78), rw, s(110)),
             "sm", c["text"] if share_ok else c["err"], 2, "medium")

    def _actions(self, cv, body, ay, bh, src, sm, share, share_ok) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        status = src["status"]
        if status == "countdown":
            cv.button("cancel_auto", (body.x, ay, s(640), bh),
                      f"Cancel auto-import ({src.get('countdown') or 0} s)",
                      lambda: app.command("cancel", source=self.sid), "danger", icon="cross",
                      size="lg")
            return
        if status == "queued":
            cv.button("cancel_q", (body.x, ay, s(520), bh), "Remove from queue",
                      lambda: app.command("cancel", source=self.sid), "secondary", icon="cross")
            return
        if status == "importing":
            cv.button("show_progress", (body.x, ay, s(520), bh), "Show progress",
                      lambda: app.open_progress(), "primary", icon="chevron")
            return
        eject = (body.right - s(300), ay, s(300), bh)
        if status == "done":
            cv.button("rescan", (body.x, ay, s(440), bh), "Scan again",
                      lambda: app.command("rescan", source=self.sid), "secondary",
                      icon="refresh")
            cv.button("eject", eject, "Eject", lambda: app.eject(self.sid), "secondary",
                      icon="eject")
            return
        cv.button("eject", eject, "Eject", lambda: app.eject(self.sid), "secondary",
                  icon="eject")
        if not share_ok:
            # the import buttons would be disabled: say why, in their place
            reason = share.get("reason") or "not configured"
            box = pygame.Rect(body.x, ay, eject[0] - body.x - s(40), bh)
            gfx.rrect(cv.surf, box, c["err_soft"], s(22))
            gfx.circle(cv.surf, (box.x + s(62), box.centery), s(34), c["surface"])
            gfx.icon(cv.surf, "warning", (box.x + s(62), box.centery), s(40), c["err"])
            gfx.text(cv.surf, t, "Import disabled", (box.x + s(120), box.centery - s(2)), "md",
                     c["err"], "semibold", "bottomleft")
            gfx.text(cv.surf, t, f"The network share is offline: {reason}",
                     (box.x + s(120), box.centery + s(4)), "sm", c["text2"], "regular",
                     max_w=box.right - box.x - s(150), min_size="xs")
            return
        can_import = status in ("ready", "countdown", "done", "error")
        cv.button("import_new", (body.x, ay, s(640), bh),
                  "Import new files" if sm["new_files"] else "Nothing new to import",
                  lambda: app.start_import(self.sid, "new", self.verify), "primary",
                  icon="update" if sm["new_files"] else None,
                  enabled=can_import and sm["new_files"] > 0, size="lg",
                  sub=f"{sm['new_files']} files · {human(sm['new_bytes'])}"
                  if sm["new_files"] else None)
        cv.button("import_all", (body.x + s(680), ay, s(460), bh), "Import all",
                  lambda: app.start_import(self.sid, "all", self.verify), "secondary",
                  enabled=can_import and sm["files"] > 0,
                  sub=f"{sm['files']} files · {human(sm['bytes'])}")

    def _set_verify(self, v: str) -> None:
        self.verify = v


class ProgressScreen(Screen):
    name = "progress"
    default_focus = "hide"

    def on_state(self, app, state) -> None:
        if not state.get("active"):
            app.pop(self)

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        st = app.state or {}
        a = st.get("active") or {}
        src = next((x for x in st.get("sources", []) if x["id"] == a.get("source")), {})
        pad = s(56)
        area = rect.inflate(-2 * pad, -2 * s(36))
        gfx.text(cv.surf, t, "IMPORTING", (area.x, area.y), "xs", c["text3"], "semibold")
        gfx.text(cv.surf, t, src.get("name", ""), (area.x, area.y + s(34)), "xl", c["text"],
                 "semibold", max_w=area.w - s(700))
        # steps
        phase = a.get("phase", "starting")
        verify_full = src.get("summary") is not None and phase == "verify" or \
            (a.get("bytes_total") and phase in ("verify",))
        steps = [("scan", "Scan"), ("copy", "Copy")]
        if verify_full or phase == "verify":
            steps.append(("verify", "Verify"))
        steps.append(("finishing", "Finish"))
        order = [k for k, _ in steps]
        cur = order.index(phase) if phase in order else (1 if phase == "starting" else 1)
        # stepper: numbered circles joined by a line, on the title's line
        d, gap, link = s(46), s(14), s(64)
        widths = [d + gap + gfx.text_width(t, label, "sm", "semibold") for _, label in steps]
        x = area.right - sum(widths) - link * (len(steps) - 1)
        cy = area.y + s(68)
        for i, (_, label) in enumerate(steps):
            done, active = i < cur, i == cur
            center = (x + d // 2, cy)
            if done:
                gfx.circle(cv.surf, center, d // 2, c["ok"])
                gfx.icon(cv.surf, "check", center, s(28), c["on_ok"])
            elif active:
                gfx.circle(cv.surf, center, d // 2, c["accent"])
                gfx.text(cv.surf, t, str(i + 1), center, "sm", c["on_accent"], "bold", "center")
            else:
                gfx.circle(cv.surf, center, d // 2, c["border"])
                gfx.circle(cv.surf, center, d // 2 - s(3), c["bg"])
                gfx.text(cv.surf, t, str(i + 1), center, "sm", c["text3"], "semibold", "center")
            col = c["ok"] if done else c["text"] if active else c["text3"]
            gfx.text(cv.surf, t, label, (x + d + gap, cy), "sm", col,
                     "bold" if active else "semibold", "midleft")
            x += widths[i]
            if i < len(steps) - 1:
                pygame.draw.line(cv.surf, c["ok"] if done else c["border"],
                                 (x + s(14), cy), (x + link - s(14), cy), s(3))
                x += link
        # hero card
        card = pygame.Rect(area.x, area.y + s(130), area.w, s(410))
        gfx.card(cv.surf, card, t, s(36))
        pct = a.get("percent", 0.0)
        cx = card.x + s(56)
        r = gfx.text(cv.surf, t, f"{pct:.0f}%", (cx, card.y + s(20)), s(170), c["accent"],
                     "bold")
        label = {"copy": "copied", "verify": "verified", "finishing": "finishing",
                 "starting": "starting"}.get(phase, phase)
        base = gfx.baseline(t, r, s(170), "bold")
        gfx.text(cv.surf, t, label, (r.right + s(24), base), "xl", c["text2"], "medium",
                 "baseline")
        eta = a.get("eta")
        gfx.text(cv.surf, t, duration(eta) if eta is not None else "",
                 (card.right - s(56), base), "xl", c["text"], "semibold", "baseline_right")
        gfx.text(cv.surf, t, "left" if eta is not None else "", (card.right - s(56),
                 base - s(64)), "sm", c["text3"], "semibold", "baseline_right")
        gfx.progress(cv.surf, (cx, r.bottom + s(12), card.w - s(112), s(30)), t, pct / 100)
        cur_file = a.get("current_file") or ""
        gfx.text(cv.surf, t, cur_file, (cx, r.bottom + s(66)), "md", c["text2"],
                 max_w=card.w - s(112))
        # stats: the destination card is wider, the folder name must fit
        sy = card.bottom + s(28)
        gap = s(28)
        unit = (area.w - 3 * gap) / 4.6
        share = st.get("share", {})
        where = " › ".join(x for x in (share.get("server"), (share.get("path") or "").strip("/"),
                                       (share.get("subdir") or "").strip("/")) if x)
        stats = [("SPEED", f"{human(a.get('speed_now'))}/s",
                  f"average {human(a.get('speed_avg'))}/s" if a.get("speed_avg") else "", 1),
                 ("FILES", f"{a.get('files_done', 0)} / {a.get('files_total', 0)}", "", 1),
                 ("DATA", human(a.get("bytes_done")), f"of {human(a.get('bytes_total'))}", 1),
                 ("DESTINATION", a.get("folder") or "…", where, 1.6)]
        x = area.x
        for i, (k, v, sub, weight) in enumerate(stats):
            w = area.right - x if i == len(stats) - 1 else int(unit * weight)
            r2 = pygame.Rect(x, sy, w, s(176))
            x += w + gap
            gfx.card(cv.surf, r2, t, s(28))
            gfx.text(cv.surf, t, k, (r2.x + s(32), r2.y + s(24)), "xs", c["text3"], "semibold")
            gfx.text(cv.surf, t, v, (r2.x + s(32), r2.y + s(58)), "lg", c["text"], "semibold",
                     max_w=r2.w - s(64), min_size="sm")
            if sub:
                gfx.text(cv.surf, t, sub, (r2.x + s(32), r2.y + s(118)), "sm", c["text2"],
                         max_w=r2.w - s(64))
        # actions
        by = area.bottom - s(100)
        cv.button("hide", (area.x, by, s(380), s(100)), "Back to home", lambda: app.pop(self),
                  "secondary", icon="back")
        cv.button("cancel", (area.right - s(380), by, s(380), s(100)), "Cancel import",
                  lambda: app.confirm("Cancel the import?",
                                      "Files already copied are kept and will be recognised "
                                      "next time.", "Cancel import",
                                      lambda: app.command("cancel", source=a.get("source", "")),
                                      cancel="Keep importing"),
                  "danger_ghost", icon="cross")
        q = st.get("queue", [])
        if q:
            gfx.text(cv.surf, t, f"{len(q)} more waiting", (area.centerx, by + s(50)), "md",
                     c["text2"], anchor="center")


class PauseScreen(Screen):
    """The share was lost during a copy: nothing happens until the user
    chooses (no automatic retries)."""
    name = "pause"
    default_focus = "retry"

    def on_state(self, app, state) -> None:
        if not state.get("decision"):
            app.pop(self)

    def on_nav(self, app, action: str) -> bool:
        return action == "back"   # a decision is required

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        err = ((app.state or {}).get("decision") or {}).get("error", "")
        cv.surf.fill(c["err_soft"], rect)
        cx = rect.centerx
        gfx.circle(cv.surf, (cx, rect.y + s(230)), s(96), c["err"])
        gfx.icon(cv.surf, "warning", (cx, rect.y + s(226)), s(96), c["on_err"])
        gfx.text(cv.surf, t, "Import paused", (cx, rect.y + s(390)), "xxl", c["err"], "bold",
                 "center")
        wrap(cv, err[:1].upper() + err[1:], pygame.Rect(cx - s(700), rect.y + s(470), s(1400),
                                                        s(140)), "lg", c["text"], 2, "medium",
             align="center")
        gfx.text(cv.surf, t, "Check the network share, then retry. Files already copied are safe;"
                 " the interrupted file is copied again.", (cx, rect.y + s(610)), "md",
                 c["text2"], anchor="center", max_w=s(1500))
        bw, bh = s(460), s(124)
        cv.button("retry", (cx - bw - s(20), rect.y + s(700), bw, bh), "Retry",
                  lambda: app.command("retry"), "danger", icon="refresh", size="lg")
        cv.button("abort", (cx + s(20), rect.y + s(700), bw, bh), "Cancel import",
                  lambda: app.command("decide", choice="cancel"), "secondary", icon="cross",
                  size="lg")


class ResultScreen(Screen):
    name = "result"
    default_focus = "ok"

    def __init__(self, result: dict):
        self.result = result

    def on_state(self, app, state) -> None:
        lr = state.get("last_result")
        if not lr or lr.get("time") != self.result.get("time"):
            app.pop(self)
            return
        # success: the card was removed -> done
        if self.result.get("status") == "done" and not any(
                x["id"] == self.result.get("source") for x in state.get("sources", [])):
            app.dismiss_result()

    def on_nav(self, app, action: str) -> bool:
        if action in ("back", "ok"):
            app.dismiss_result()
            return True
        return False

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        r = self.result
        ok = r.get("status") == "done"
        cancelled = r.get("status") == "cancelled" or "cancelled" in (r.get("error") or "")
        tone = "ok" if ok else "warn" if cancelled else "err"
        main = c[tone]
        cv.surf.fill(c[f"{tone}_soft"], rect)
        cx = rect.centerx
        gfx.circle(cv.surf, (cx, rect.y + s(240)), s(120), main)
        gfx.icon(cv.surf, "check" if ok else "cross", (cx, rect.y + s(240)), s(130),
                 c.get(f"on_{tone}", (255, 255, 255)))
        title = "Import complete" if ok else ("Import cancelled" if cancelled
                                              else "Import failed")
        gfx.text(cv.surf, t, title, (cx, rect.y + s(440)), "xxl", main, "bold", "center")
        name = r.get("name", "")
        line = f"{name}: {r.get('files', 0)} files · {human(r.get('bytes'))}"
        if r.get("seconds"):
            line += f" in {duration(r['seconds'])}"
            if r.get("mbps"):
                line += f" · {r['mbps']} MB/s"
        gfx.text(cv.surf, t, line, (cx, rect.y + s(540)), "lg", c["text"], "medium", "center",
                 max_w=rect.w - s(200))
        if r.get("folder"):
            gfx.text(cv.surf, t, f"Folder: {r['folder']}", (cx, rect.y + s(606)), "md",
                     c["text2"], anchor="center", max_w=rect.w - s(200))
        if ok:
            hint = ("You can disconnect the camera" if r.get("source", "").startswith("cam:")
                    else "You can remove the card")
            gfx.text(cv.surf, t, hint, (cx, rect.y + s(690)), "lg", main, "semibold",
                     "center")
        if not ok and r.get("error"):
            msg = r["error"]
            if cancelled:
                msg = "Files already copied are kept and will be recognised next time."
            wrap(cv, msg, pygame.Rect(cx - s(700), rect.y + s(660), s(1400), s(120)),
                 "md", c["text2"] if cancelled else c["err"], 2, "medium", align="center")
        cv.button("ok", (cx - s(220), rect.y + s(800), s(440), s(120)), "OK",
                  lambda: app.dismiss_result(),
                  "success" if ok else "primary" if cancelled else "danger", size="lg")
