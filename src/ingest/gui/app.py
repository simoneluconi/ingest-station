"""ingest-gui: full-screen interface, a client of ingestd.

The main loop sleeps in select() on the input devices and on a wake-up pipe
fed by the daemon listener and the command workers; a frame is rendered
only when something changed (and ~10 times per second while an animation
runs). The GUI can crash or restart without affecting the imports.
"""

from __future__ import annotations

import concurrent.futures
import os
import queue
import select
import signal
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")   # no SDL audio (it grabs the HDMI output)
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402
import pygame.freetype  # noqa: E402

from ingest.common.client import Client, DaemonError  # noqa: E402

from . import drm, gfx  # noqa: E402
from .dialogs import Confirm, Message  # noqa: E402
from .inputdev import KEYPAD_NAV, Inputs  # noqa: E402
from .screens import Home, PauseScreen, ProgressScreen, ResultScreen, SourceScreen, human  # noqa: E402
from .settings import History, Settings, Wizard  # noqa: E402
from .theme import Theme  # noqa: E402
from .ui import Canvas, move_focus  # noqa: E402

HEARTBEAT = "/run/ingest-gui.alive"
SCREENSHOT = "/tmp/ingest-gui.png"
DEV_COMMANDS = "/tmp/ingest-gui.cmd"     # development aid, see _dev_commands()
STATUS_H = 104


class Api:
    """Daemon connection: a listener thread for state snapshots and a small
    pool for commands; results are handed to the main loop via a queue."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3,
                                                          thread_name_prefix="gui-cmd")
        threading.Thread(target=self._listen, daemon=True, name="gui-events").start()

    def _wake(self):
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def _listen(self):
        while True:
            try:
                with Client(timeout=10) as c:
                    for st in c.events():
                        self.q.put(("state", st))
                        self._wake()
            except Exception:  # noqa: BLE001 - daemon restarting: retry
                self.q.put(("offline", None))
                self._wake()
                time.sleep(2)

    def call(self, cmd: str, cb=None, **args):
        def run():
            try:
                with Client(timeout=300) as c:
                    res = c.call(cmd, **args)
                self.q.put(("result", cb, True, res))
            except (DaemonError, OSError) as e:
                self.q.put(("result", cb, False, e))
            self._wake()
        self.pool.submit(run)


class App:
    def __init__(self):
        pygame.freetype.init()   # only fonts: no display, no audio subsystems
        self.display = None
        self.width, self.height = 1920, 1080
        self._open_display()
        self.surf = pygame.Surface((self.width, self.height), 0, 32)
        self.settings: dict = {}
        self.theme = Theme("light", self.width, self.height)
        self.inputs = Inputs(self.width, self.height)
        self.api = Api()
        self.state: dict | None = None
        self.prev_state: dict | None = None
        self.stack: list = [Home()]
        self.focus: dict[int, str] = {}
        self.widgets: list = []
        self.dirty = True
        self.animating = False
        self.last_input = ""
        self.last_nav_code = 0
        self.seen_ready: set[str] = set()
        self.seen_result = None
        self.wizard_shown = False
        self.screenshot = False
        self._last_minute = -1
        self._last_beat = 0.0
        self._last_hotplug = 0.0
        signal.signal(signal.SIGUSR1, lambda *_: self._want_screenshot())
        signal.signal(signal.SIGUSR2, lambda *_: self._want_dev_commands())
        self.dev_pending = False
        self.command("settings.get", self._got_settings)

    # -- display -------------------------------------------------------------
    def _open_display(self) -> None:
        try:
            self.display = drm.open_display()
            self.width, self.height = self.display.width, self.display.height
        except drm.DisplayError:
            self.display = None

    def _check_hotplug(self) -> None:
        if time.monotonic() - self._last_hotplug < 3:
            return
        self._last_hotplug = time.monotonic()
        name = drm.DrmDisplay.connected_connector()
        current = self.display.connector_name if self.display else None
        if name == current:
            return
        if self.display:
            self.display.close()
        self._open_display()
        self.surf = pygame.Surface((self.width, self.height), 0, 32)
        self.theme = Theme(self.theme.name, self.width, self.height)
        self.inputs.resize(self.width, self.height)
        self.dirty = True

    def _want_dev_commands(self):
        self.dev_pending = True
        self.api._wake()

    def _dev_commands(self) -> None:
        """Development aid over SSH: navigation actions written one per line
        in /tmp/ingest-gui.cmd (up/down/left/right/ok/back/home/settings,
        "focus:<key>", "screen:settings|history|home") then SIGUSR2."""
        from .inputdev import Event
        self.dev_pending = False
        try:
            with open(DEV_COMMANDS, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            os.remove(DEV_COMMANDS)
        except OSError:
            return
        for ln in lines:
            if ln.startswith("focus:"):
                self.focus[id(self.top())] = ln[6:]
            elif ln == "screen:settings":
                self.open_settings()
            elif ln == "screen:history":
                self.open_history()
            elif ln == "offline" and self.state:   # preview: share offline until the next event
                self.state["share"] = {**self.state.get("share", {}), "state": "offline",
                                       "reason": "host unreachable"}
            elif ln.startswith("msg:"):      # sample dialog: msg:error|ok|info
                self.push(Message("Cannot start the import",
                                  "the network share is offline: host unreachable",
                                  ln[4:] if ln[4:] in ("error", "ok", "info") else "info"))
            elif ln.startswith("cat:") and isinstance(self.top(), Settings):
                self.top()._select(self, ln[4:])
                self.focus[id(self.top())] = ln
            else:
                self._handle(Event("nav", action=ln))
            self._render()
        self.dirty = True

    def _want_screenshot(self):
        self.screenshot = True
        self.dirty = True
        self.api._wake()

    # -- screens -------------------------------------------------------------
    def top(self):
        return self.stack[-1]

    def push(self, screen) -> None:
        self.stack.append(screen)
        self.dirty = True

    def pop(self, screen=None) -> None:
        if screen is None:
            screen = self.top()
        if screen in self.stack and len(self.stack) > 1:
            self.stack.remove(screen)
            self.focus.pop(id(screen), None)
        self.dirty = True

    def focus_key(self) -> str | None:
        return self.focus.get(id(self.top()))

    def open_source(self, sid: str) -> None:
        self.push(SourceScreen(sid, self.settings.get("verify_default", "hash")))

    def open_progress(self) -> None:
        if not any(isinstance(s, ProgressScreen) for s in self.stack):
            self.push(ProgressScreen())

    def open_settings(self) -> None:
        self.push(Settings(self))

    def open_history(self) -> None:
        self.push(History())

    def confirm(self, title, message, label, fn, danger=True, cancel="Cancel") -> None:
        self.push(Confirm(title, message, label, fn, danger, cancel))

    def command(self, cmd: str, cb=None, **args) -> None:
        self.api.call(cmd, cb, **args)

    def start_import(self, sid: str, mode: str, verify: str) -> None:
        def done(ok, res):
            if not ok:
                self.push(Message("Cannot start the import", str(res), "error"))
        self.command("import", done, source=sid, mode=mode, verify=verify)

    def eject(self, sid: str) -> None:
        self.command("eject", source=sid)
        for s in list(self.stack):
            if isinstance(s, SourceScreen) and s.sid == sid:
                self.pop(s)

    def set_setting(self, changes: dict) -> None:
        _merge(self.settings, changes)          # immediate feedback
        self._apply_settings()
        self.command("settings.set", self._got_settings, changes=changes)

    def _got_settings(self, ok, res) -> None:
        if ok and isinstance(res, dict):
            self.settings = res
            self._apply_settings()

    def _apply_settings(self) -> None:
        gui = self.settings.get("gui", {})
        if gui.get("theme", "light") != self.theme.name:
            self.theme = Theme(gui.get("theme", "light"), self.width, self.height)
        self.inputs.layout = gui.get("keyboard", "it")
        self.dirty = True

    def dismiss_result(self) -> None:
        self.command("dismiss")
        for s in list(self.stack):
            if isinstance(s, ResultScreen):
                self.pop(s)

    def finish_wizard(self, wiz) -> None:
        self.set_setting({"gui": {"setup_done": True}})
        self.pop(wiz)

    # -- state ---------------------------------------------------------------
    def _on_state(self, st: dict) -> None:
        prev, self.prev_state, self.state = self.state, self.state, st
        if st.get("settings", {}).get("gui") != (prev or {}).get("settings", {}).get("gui"):
            self.command("settings.get", self._got_settings)
        if st.get("decision") and not any(isinstance(s, PauseScreen) for s in self.stack):
            self.push(PauseScreen())
        lr = st.get("last_result")
        if lr and lr.get("time") != self.seen_result and not st.get("decision"):
            self.seen_result = lr.get("time")
            for s in list(self.stack):
                if isinstance(s, (ProgressScreen, SourceScreen)):
                    self.stack.remove(s)
            self.push(ResultScreen(lr))
        if st.get("active") and not (prev or {}).get("active") and \
                isinstance(self.top(), (Home, SourceScreen)):
            self.open_progress()
        ids = {x["id"] for x in st.get("sources", [])}
        self.seen_ready &= ids
        for src in st.get("sources", []):
            if src["status"] in ("ready", "countdown") and src["id"] not in self.seen_ready:
                self.seen_ready.add(src["id"])
                if isinstance(self.top(), Home):
                    self.open_source(src["id"])
        share = st.get("share", {})
        if share.get("state") == "unconfigured" and not self.wizard_shown and \
                self.settings and not self.settings.get("gui", {}).get("setup_done") and \
                isinstance(self.top(), Home):
            self.wizard_shown = True
            self.push(Wizard(self))
        for s in list(self.stack):
            s.on_state(self, st)
        self.dirty = True

    # -- input ---------------------------------------------------------------
    def _handle(self, ev) -> None:
        top = self.top()
        self.dirty = True
        if ev.kind == "move":
            self.last_input = ev.pointer
            return
        if ev.kind == "key":
            self.last_input = "keyboard"
            return
        if ev.kind == "power":
            if not any(isinstance(s, Confirm) for s in self.stack):
                self.confirm("Shut down the station?", "Press the power button again to "
                             "switch it on.", "Shut down",
                             lambda: self.command("system.poweroff"))
            return
        if ev.kind == "char":
            top.on_char(self, ev.char)
            return
        if ev.kind == "scroll":
            top.on_nav(self, "pagedown" if ev.dy > 0 else "pageup")
            return
        if ev.kind == "click":
            self.last_input = ev.pointer
            for w in reversed(self.widgets):
                if w.rect.collidepoint(ev.x, ev.y):
                    if w.enabled:
                        self.focus[id(top)] = w.key
                        if w.on_activate:
                            w.on_activate()
                    return
            return
        if ev.kind == "nav":
            if getattr(top, "accepts_text", False) and ev.code in KEYPAD_NAV:
                return   # digits typed on a numeric keypad
            self.last_nav_code = ev.code
            if top.on_nav(self, ev.action):
                return
            cur = next((w for w in self.widgets if w.key == self.focus_key()), None)
            if ev.action in ("up", "down", "left", "right", "next"):
                if cur and cur.on_nav and cur.on_nav(ev.action):
                    return
                nxt = move_focus(self.widgets, cur, ev.action)
                if nxt:
                    self.focus[id(top)] = nxt.key
            elif ev.action == "ok":
                if cur and cur.enabled and cur.on_activate:
                    cur.on_activate()
            elif ev.action == "settings" and isinstance(top, Home):
                self.open_settings()
            elif ev.action == "home":
                while len(self.stack) > 1 and not isinstance(self.top(),
                                                              (PauseScreen, ResultScreen)):
                    self.stack.pop()

    # -- rendering -----------------------------------------------------------
    def _render(self) -> None:
        t, c = self.theme, self.theme.c
        self.animating = False
        self.surf.fill(c["bg"])
        self._status_bar()
        main = pygame.Rect(0, t.s(STATUS_H), self.width, self.height - t.s(STATUS_H))
        # the last non-modal screen, then the modal ones above it
        base = max(i for i, s in enumerate(self.stack) if not s.modal)
        widgets = []
        for scr in self.stack[base:]:
            cv = Canvas(self, self.surf)
            scr.draw(cv, main)
            widgets = cv.widgets
        self.widgets = widgets
        top = self.top()
        keys = {w.key for w in widgets if w.focusable and w.enabled}
        if self.focus.get(id(top)) not in keys:
            d = top.default_focus if top.default_focus in keys else next(
                (w.key for w in widgets if w.focusable and w.enabled), None)
            if d:
                self.focus[id(top)] = d
        cur = next((w for w in widgets if w.key == self.focus.get(id(top))), None)
        if cur is not None:
            ring = cur.rect.inflate(t.s(14), t.s(14))
            rad = cur.radius + t.s(7) if cur.radius else t.s(12)
            _ring(self.surf, ring, c["focus"], rad, t.s(5))
        if self.inputs.pointer_visible and time.monotonic() - self.inputs.last_pointer_move < 6:
            x, y = self.inputs.pointer
            gfx.circle(self.surf, (x, y), t.s(14), (255, 255, 255))
            gfx.circle(self.surf, (x, y), t.s(10), c["accent"])
            self.animating = True
        if self.display is not None:
            self.display.present(self.surf.get_buffer().raw)
        if self.screenshot:
            self.screenshot = False
            try:
                gfx.save_png(self.surf, SCREENSHOT)
            except Exception:  # noqa: BLE001
                pass

    def _status_bar(self) -> None:
        t, c, s = self.theme, self.theme.c, self.theme.s
        st = self.state or {}
        h = s(STATUS_H)
        pygame.draw.rect(self.surf, c["surface"], (0, 0, self.width, h))
        pygame.draw.line(self.surf, c["border"], (0, h - 1), (self.width, h - 1), s(2))
        x = s(56)
        mark = pygame.Rect(x, (h - s(60)) // 2, s(60), s(60))
        gfx.rrect(self.surf, mark, c["accent"], s(18))
        gfx.icon(self.surf, "update", mark.center, s(34), c["on_accent"])
        r = gfx.text(self.surf, t, "Ingest Station", (mark.right + s(20), h // 2), "md",
                     c["text"], "semibold", "midleft")
        # share
        share = st.get("share", {})
        state = share.get("state", "unknown") if self.state else "daemon"
        if state == "online":
            label, key = f"{share.get('server', '')} · {human(share.get('free'))} free", "ok"
        elif state == "unconfigured":
            label, key = "No destination configured", "warn"
        elif state == "daemon":
            label, key = "Starting…", "text3"
        else:
            label, key = "Share offline", "err"
        soft = {"ok": "ok_soft", "warn": "warn_soft", "err": "err_soft"}.get(key, "surface2")
        pill_w = gfx.text_width(t, label, "sm", "medium") + s(76)
        pill = pygame.Rect(r.right + s(48), (h - s(58)) // 2, pill_w, s(58))
        gfx.rrect(self.surf, pill, c[soft], s(29))
        gfx.circle(self.surf, (pill.x + s(30), pill.centery), s(9), c[key])
        gfx.text(self.surf, t, label, (pill.x + s(52), pill.centery), "sm", c[key], "medium",
                 "midleft")
        # right side: clock, IP, version
        now = time.localtime()
        clock = time.strftime("%H:%M", now)
        cr = gfx.text(self.surf, t, clock, (self.width - s(56), h // 2), "lg", c["text"],
                      "semibold", "midright")
        ip = st.get("ip") or "no network"
        ir = gfx.text(self.surf, t, ip, (cr.x - s(44), h // 2), "sm", c["text2"], "medium",
                      "midright")
        gfx.icon(self.surf, "network", (ir.x - s(26), h // 2), s(32), c["text3"])
        gfx.text(self.surf, t, f"v{st.get('version', '')}", (ir.x - s(80), h // 2), "xs",
                 c["text3"], "regular", "midright")
        self._last_minute = now.tm_min

    # -- main loop -----------------------------------------------------------
    def run(self) -> None:
        while True:
            self.inputs.scan()
            fds = [self.api.wake_r] + self.inputs.fds()
            timeout = 0.1 if self.animating else 1.0
            try:
                ready, _, _ = select.select(fds, [], [], timeout)
            except (OSError, ValueError):
                ready = []
                self.inputs.scan(force=True)
            for fd in ready:
                if fd == self.api.wake_r:
                    try:
                        os.read(fd, 4096)
                    except OSError:
                        pass
                else:
                    for ev in self.inputs.read(fd):
                        self._handle(ev)
            while True:
                try:
                    kind, *rest = self.api.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "state":
                    self._on_state(rest[0])
                elif kind == "offline":
                    self.state = None
                    self.dirty = True
                elif kind == "result":
                    cb, ok, res = rest
                    if cb:
                        try:
                            cb(ok, res)
                        except Exception:  # noqa: BLE001 - a bad callback must not kill the GUI
                            pass
                    self.dirty = True
            if self.dev_pending:
                self._dev_commands()
            if time.localtime().tm_min != self._last_minute:
                self.dirty = True
            if self.dirty or self.animating:
                self.dirty = False
                self._render()
            self._check_hotplug()
            self._heartbeat()

    def _heartbeat(self) -> None:
        if time.monotonic() - self._last_beat < 5:
            return
        self._last_beat = time.monotonic()
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass


def _ring(surf, rect, color, radius, width) -> None:
    """Focus ring: an anti-aliased outline drawn on top of the widget."""
    r = pygame.Rect(rect)
    # transparent pixels already in the ring colour, and the hole subtracts
    # alpha only: otherwise the anti-aliased edges turn dark (a grey halo)
    ring = pygame.Surface(r.size, pygame.SRCALPHA)
    ring.fill((*color[:3], 0))
    gfx.rrect(ring, ring.get_rect(), (*color[:3], 255), radius)
    inner = ring.get_rect().inflate(-2 * width, -2 * width)
    hole = pygame.Surface(inner.size, pygame.SRCALPHA)
    gfx.rrect(hole, hole.get_rect(), (0, 0, 0, 255), max(0, radius - width))
    ring.blit(hole, inner.topleft, special_flags=pygame.BLEND_RGBA_SUB)
    surf.blit(ring, r.topleft)


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def main() -> int:
    App().run()
    return 0
