"""Settings, first-boot wizard and import history."""

from __future__ import annotations

import copy
import datetime as dt

import pygame

from . import gfx
from .dialogs import Choice, Confirm, FolderBrowser, Message, TextInput
from .screens import human, wrap
from .ui import Canvas, Screen

SCHEMAS = [("{date}_{time}_{label}", "2026-09-26_1405_EOS_R6", None),
           ("{label}_{date}_{time}", "EOS_R6_2026-09-26_1405", None),
           ("{date}_{label}", "2026-09-26_EOS_R6", None),
           ("{year}-{month}_{label}", "2026-09_EOS_R6", None)]
NFS_VERSIONS = [("auto", "Automatic", "newest version the server accepts"),
                ("4.2", "NFS 4.2", None), ("4.1", "NFS 4.1", None), ("4.0", "NFS 4.0", None),
                ("3", "NFS 3", None)]
SMB_VERSIONS = [("auto", "Automatic", "newest version the server accepts"),
                ("3.1.1", "SMB 3.1.1", None), ("3.0", "SMB 3.0", None), ("2.1", "SMB 2.1", None)]
CATEGORIES = [("dest", "Destination", "folder"), ("import", "Import", "update"),
              ("display", "Display", "sun"), ("network", "Network", "network"),
              ("web", "Web access", "lock"), ("system", "System", "gear")]


class ShareForm:
    """Destination fields shared by Settings and the wizard."""

    def __init__(self, share: dict):
        self.draft = copy.deepcopy(share)
        self.password: str | None = None
        self.test: dict | None = None
        self.testing = False

    def rows(self, cv: Canvas, x: int, y: int, w: int, prefix: str = "") -> int:
        app, s, c = cv.app, cv.s, cv.c
        d = self.draft
        rh = s(104)
        smb = d.get("type") == "smb"
        cv.segmented(prefix + "type", (x, y, w, s(104)),
                     [("nfs", "NFS", "fast, recommended"), ("smb", "SMB", "Windows, users")],
                     d.get("type", "nfs"), self._set("type"))
        y += s(130)

        def edit(key, title, hint="", password=False):
            def go():
                def ok(v):
                    if password:
                        self.password = v
                    else:
                        d[key] = v.strip()
                    self.test = None
                app.push(TextInput(title, "" if password else d.get(key, ""), ok,
                                   password=password, hint=hint))
            return go
        cv.row(prefix + "server", (x, y, w, rh), "Server", d.get("server") or "not set",
               edit("server", "Server address or name", "e.g. 192.168.1.10 or nas.local"),
               icon="network")
        y += rh
        cv.row(prefix + "path", (x, y, w, rh), "Share name" if smb else "Export path",
               d.get("path") or "not set",
               edit("path", "Share name" if smb else "NFS export path",
                    "e.g. Photos" if smb else "e.g. /volume1/photos"), icon="folder")
        y += rh
        if smb:
            cv.row(prefix + "user", (x, y, w, rh), "User", d.get("username") or "guest",
                   edit("username", "SMB user name"), icon="lock")
            y += rh
            cv.row(prefix + "password", (x, y, w, rh), "Password",
                   "changed" if self.password else "unchanged",
                   edit("password", "SMB password", password=True), icon="lock")
            y += rh
        return y

    def cells(self, app) -> list[tuple]:
        """(key, label, value, action, icon) for the share fields."""
        d = self.draft
        smb = d.get("type") == "smb"

        def edit(key, title, hint="", password=False):
            def go():
                def ok(v):
                    if password:
                        self.password = v
                    else:
                        d[key] = v.strip()
                    self.test = None
                app.push(TextInput(title, "" if password else d.get(key, ""), ok,
                                   password=password, hint=hint))
            return go
        out = [("server", "Server", d.get("server") or "not set",
                edit("server", "Server address or name", "e.g. 192.168.1.10 or nas.local"),
                "network"),
               ("path", "Share name" if smb else "Export path", d.get("path") or "not set",
                edit("path", "Share name" if smb else "NFS export path",
                     "e.g. Photos" if smb else "e.g. /volume1/photos"), "folder")]
        if smb:
            out += [("user", "User", d.get("username") or "guest",
                     edit("username", "SMB user name"), "lock"),
                    ("password", "Password", "changed" if self.password else "unchanged",
                     edit("password", "SMB password", password=True), "lock")]
        return out

    def _set(self, key):
        def f(v):
            self.draft[key] = v
            self.test = None
            if key == "type":
                self.draft["version"] = "auto"
        return f

    def run_test(self, app) -> None:
        self.testing, self.test = True, None

        def done(ok, res):
            self.testing = False
            self.test = res if ok else {"state": "offline", "reason": str(res)}
        args = {"share": self.draft}
        if self.password is not None:
            args["password"] = self.password
        app.command("share.test", done, **args)

    def test_line(self, cv: Canvas, x: int, y: int, w: int) -> None:
        c = cv.c
        if self.testing:
            gfx.text(cv.surf, cv.t, "Testing the connection…", (x, y), "md", c["text2"])
        elif self.test:
            if self.test.get("state") == "online":
                msg = f"Connected ({self.test.get('protocol', '')}), {human(self.test.get('free'))} free"
                gfx.icon(cv.surf, "check", (x + cv.s(20), y + cv.s(20)), cv.s(40), c["ok"])
                gfx.text(cv.surf, cv.t, msg, (x + cv.s(56), y), "md", c["ok"], "semibold",
                         max_w=w - cv.s(56))
            else:
                gfx.icon(cv.surf, "warning", (x + cv.s(20), y + cv.s(20)), cv.s(40), c["err"])
                wrap(cv, self.test.get("reason") or "failed", pygame.Rect(x + cv.s(56), y,
                                                                         w - cv.s(56), cv.s(90)),
                     "md", c["err"], 2, "medium")

    def save(self, app, done=None) -> None:
        args = {"changes": {"share": self.draft}}
        if self.password is not None:
            args["smb_password"] = self.password

        def cb(ok, res):
            if not ok:
                app.push(Message("Not saved", str(res), "error"))
            elif done:
                done()
        app.command("settings.set", cb, **args)


class Settings(Screen):
    name = "settings"
    default_focus = "cat:dest"

    def __init__(self, app):
        self.cat = "dest"
        self.form = ShareForm((app.state or {}).get("share", {}) | {
            k: app.settings.get("share", {}).get(k, "")
            for k in ("type", "server", "path", "subdir", "username", "version")})
        self.slots = None
        self.network = None
        self.static = {}

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        pad = s(56)
        area = rect.inflate(-2 * pad, -2 * s(36))
        nav = pygame.Rect(area.x, area.y + s(120), s(420), area.h - s(120))
        panel = pygame.Rect(nav.right + s(40), area.y + s(120), area.right - nav.right - s(40),
                            area.h - s(120))
        # header: "Back" on the categories' columns, the title on the panel's
        cv.back("back", (area.x, area.y, s(220), s(88)), lambda: app.pop(self))
        gfx.text(cv.surf, t, "Settings", (panel.x + s(56), area.y + s(44)), "xl", c["text"],
                 "semibold", "midleft")
        for i, (key, label, icon) in enumerate(CATEGORIES):
            r = pygame.Rect(nav.x, nav.y + i * s(100), nav.w, s(88))
            if key == self.cat:
                gfx.rrect(cv.surf, r, c["accent_soft"], s(20))
            col = c["accent"] if key == self.cat else c["text2"]
            gfx.icon(cv.surf, icon, (r.x + s(46), r.centery), s(38), col)
            gfx.text(cv.surf, t, label, (r.x + s(90), r.centery), "md", col if key == self.cat
                     else c["text"], "semibold" if key == self.cat else "medium", "midleft")
            from .ui import Widget
            cv.add(Widget(f"cat:{key}", r, (lambda k=key: self._select(app, k)), True, True,
                          s(20)))
        gfx.card(cv.surf, panel, t, s(32))
        inner = panel.inflate(-s(56), -s(48))
        getattr(self, f"_draw_{self.cat}")(cv, inner)

    def _select(self, app, key):
        self.cat = key
        if key == "system":
            app.command("slots.status", lambda ok, r: setattr(self, "slots", r if ok else {}))
        if key == "network":
            def got(ok, r):
                self.network = r if ok else {}
                self.static = dict(self.network or {})
            app.command("network.get", got)

    # -- categories ------------------------------------------------------------
    def _title(self, cv, inner, title, sub=""):
        x = inner.x + cv.s(28)    # the column of the row icons and section labels
        gfx.text(cv.surf, cv.t, title, (x, inner.y), "lg", cv.c["text"], "semibold")
        if sub:
            gfx.text(cv.surf, cv.t, sub, (x, inner.y + cv.s(64)), "sm", cv.c["text2"],
                     max_w=inner.w - cv.s(56))
        return inner.y + cv.s(130 if sub else 84)

    def _section(self, cv, inner, y, label) -> int:
        """A small uppercase label above a group of controls."""
        gfx.text(cv.surf, cv.t, label.upper(), (inner.x + cv.s(28), y), "xs", cv.c["text3"],
                 "semibold")
        return y + cv.s(50)

    def _draw_dest(self, cv, inner):
        app, s = cv.app, cv.s
        y = self._title(cv, inner, "Destination")
        d = self.form.draft
        cv.segmented("type", (inner.x, y, inner.w, s(110)),
                     [("nfs", "NFS", "fast, recommended"), ("smb", "SMB", "Windows, users")],
                     d.get("type", "nfs"), self.form._set("type"))
        y += s(140)
        versions = SMB_VERSIONS if d.get("type") == "smb" else NFS_VERSIONS
        cells = self.form.cells(app) + [
            ("subdir", "Folder",
             "/" + d.get("subdir", "").strip("/") if d.get("subdir") else "share root",
             lambda: self._browse(app), "folder"),
            ("version", "Protocol",
             dict((v, lb) for v, lb, _ in versions).get(d.get("version", "auto"), "Automatic"),
             lambda: app.push(Choice("Protocol version", versions, d.get("version", "auto"),
                                     self.form._set("version"))), "gear")]
        gap = s(24)
        cw = (inner.w - gap) // 2
        rh = s(100)
        for i, (key, label, value, action, icon) in enumerate(cells):
            col, row = i % 2, i // 2
            cv.row(key, (inner.x + col * (cw + gap), y + row * rh, cw, rh - s(8)), label,
                   value, action, icon=icon)
        y += ((len(cells) + 1) // 2) * rh + s(24)
        by = inner.bottom - s(104)
        self.form.test_line(cv, inner.x, y, inner.w)
        cv.button("test", (inner.x, by, s(420), s(104)), "Test connection",
                  lambda: self.form.run_test(app), "secondary", icon="refresh",
                  enabled=not self.form.testing)
        cv.button("save", (inner.right - s(340), by, s(340), s(104)), "Save",
                  lambda: self.form.save(app, lambda: app.push(Message(
                      "Saved", "The destination has been updated.", "ok"))), "primary",
                  icon="check")

    def _browse(self, app):
        """The folder browser works on the saved share: save first."""
        def pick(path):
            self.form.draft["subdir"] = path
            self.form.save(app)
        self.form.save(app, lambda: app.push(FolderBrowser(self.form.draft.get("subdir", ""),
                                                           pick)))

    def _draw_import(self, cv, inner):
        app, s = cv.app, cv.s
        st = app.settings
        y = self._title(cv, inner, "Import")
        auto = st.get("auto_import", False)
        cv.row("auto", (inner.x, y, inner.w, s(104)), "Automatic import",
               sub="Start copying the new files after a short countdown", toggle=auto,
               action=lambda: app.set_setting({"auto_import": not auto}), icon="refresh")
        y += s(104)
        delay = st.get("auto_import_delay", 10)
        cv.row("delay", (inner.x, y, inner.w, s(96)), "Countdown", f"{delay} s",
               lambda: app.push(Choice("Countdown before automatic import",
                                       [(v, f"{v} seconds", None) for v in (5, 10, 15, 30)],
                                       delay, lambda v: app.set_setting(
                                           {"auto_import_delay": v}))),
               enabled=auto, icon="clock")
        y += s(96) + s(24)
        y = self._section(cv, inner, y, "Default verification")
        cv.segmented("vdef", (inner.x, y, inner.w, s(116)),
                     [("none", "None", "copy only"), ("hash", "Hash", "checksum while copying"),
                      ("full", "Full", "copy, then read back")],
                     st.get("verify_default", "hash"),
                     lambda v: app.set_setting({"verify_default": v}))
        y += s(116) + s(20)
        schema = st.get("folder_schema", SCHEMAS[0][0])
        cv.row("schema", (inner.x, y, inner.w, s(96)), "Folder names",
               dict((v, l) for v, l, _ in SCHEMAS).get(schema, schema),
               lambda: app.push(Choice("Folder names", SCHEMAS, schema,
                                       lambda v: app.set_setting({"folder_schema": v}))),
               icon="folder")
        y += s(96)
        beep = st.get("beep", {}).get("backend", "none")
        cv.row("beep", (inner.x, y, inner.w, s(96)), "Beeps",
               {"none": "Off", "pcspkr": "PC speaker"}.get(beep, beep),
               lambda: app.push(Choice("Beeps", [("none", "Off", None),
                                                 ("pcspkr", "PC speaker", "if the unit has one")],
                                       beep, lambda v: (app.set_setting({"beep": {"backend": v}}),
                                                        app.command("beep", backend=v,
                                                                    pattern="ok")))),
               icon="bell")

    def _draw_display(self, cv, inner):
        app, s = cv.app, cv.s
        y = self._title(cv, inner, "Display")
        y = self._section(cv, inner, y, "Theme")
        theme = app.settings.get("gui", {}).get("theme", "light")
        cv.segmented("theme", (inner.x, y, inner.w, s(110)),
                     [("light", "Light", None), ("dark", "Dark", None)], theme,
                     lambda v: app.set_setting({"gui": {"theme": v}}), size="md")
        y += s(110) + s(36)
        y = self._section(cv, inner, y, "Input")
        kb = app.settings.get("gui", {}).get("keyboard", "it")
        cv.row("keyboard", (inner.x, y, inner.w, s(96)), "Keyboard layout",
               {"it": "Italian", "us": "English (US)"}.get(kb, kb),
               lambda: app.push(Choice("Keyboard layout", [("it", "Italian", None),
                                                           ("us", "English (US)", None)], kb,
                                       lambda v: app.set_setting({"gui": {"keyboard": v}}))),
               icon="keyboard")

    def _draw_network(self, cv, inner):
        app, s, c = cv.app, cv.s, cv.c
        y = self._title(cv, inner, "Network", "The station is also reachable as ingest.local")
        ip = (app.state or {}).get("ip") or "no address"
        qr_size = s(250)
        port = app.settings.get("web", {}).get("port", 80)
        url = f"http://{ip}" + (f":{port}" if port != 80 else "")
        caption = "Scan to open the web page"
        col_w = max(qr_size, gfx.text_width(cv.t, caption, "xs"))
        qr_rect = pygame.Rect(0, y, qr_size, qr_size)
        qr_rect.centerx = inner.right - s(28) - col_w // 2
        gfx.qr(cv.surf, url, qr_rect, (17, 24, 39), (255, 255, 255))
        gfx.text(cv.surf, cv.t, caption, (qr_rect.centerx, qr_rect.bottom + s(6)), "xs",
                 c["text3"], anchor="midtop")
        ty = self._section(cv, inner, y, "IP address")
        gfx.text(cv.surf, cv.t, ip, (inner.x + s(28), ty), "xl", c["text"], "bold")
        gfx.text(cv.surf, cv.t, url, (inner.x + s(28), ty + s(84)), "md", c["accent"], "medium")
        y += s(250)
        if self.network is None:
            gfx.text(cv.surf, cv.t, "Loading…", (inner.x + s(28), y), "md", c["text2"])
            if not getattr(self, "_netreq", False):
                self._netreq = True
                self._select(app, "network")
            return
        mode = self.static.get("MODE", "dhcp")
        cv.segmented("netmode", (inner.x, y, inner.right - s(28) - col_w - inner.x - s(48),
                                 s(100)),
                     [("dhcp", "Automatic (DHCP)", None), ("static", "Static", None)], mode,
                     lambda v: self.static.__setitem__("MODE", v))
        y = max(y + s(100), qr_rect.bottom + s(50)) + s(24)
        gap, rh = s(24), s(96)
        cw = (inner.w - gap) // 2
        apply_rect = (inner.right - s(300), inner.bottom - s(100), s(300), s(100))
        if mode == "static":
            cells = (("ADDRESS", "Address", "192.168.1.50/24"),
                     ("GATEWAY", "Gateway", "192.168.1.1"),
                     ("DNS", "DNS", "192.168.1.1"))
            for i, (key, label, hint) in enumerate(cells):
                cv.row(f"net:{key}", (inner.x + (i % 2) * (cw + gap), y + (i // 2) * rh, cw,
                                      rh - s(8)), label, self.static.get(key) or "not set",
                       lambda k=key, lb=label, h=hint: app.push(TextInput(
                           lb, self.static.get(k, ""), lambda v, k=k: self.static.__setitem__(
                               k, v.strip()), hint=h)))
            apply_rect = (inner.x + cw + gap, y + rh, cw, rh - s(8))
        changed = {k: v for k, v in self.static.items()} != (self.network or {})
        cv.button("netapply", apply_rect,
                  "Apply", lambda: app.confirm(
                      "Apply the network settings?",
                      "The connection may drop for a few seconds; the new address is shown "
                      "here and on the status bar.", "Apply",
                      lambda: app.command("network.set", lambda ok, r: self._select(app,
                                                                                     "network"),
                                          **self.static), danger=False),
                  "primary", icon="check", enabled=changed)

    def _draw_web(self, cv, inner):
        app, s, c = cv.app, cv.s, cv.c
        web = app.settings.get("web", {})
        y = self._title(cv, inner, "Web access", "Monitoring from any browser on the network; "
                                                  "commands only with the PIN")
        ip = (app.state or {}).get("ip") or "–"
        port = web.get("port", 80)
        cv.row("weburl", (inner.x, y, inner.w, s(104)), "Address",
               f"http://{ip}" + (f":{port}" if port != 80 else ""), None, chevron=False,
               icon="globe")
        y += s(104)
        en = web.get("commands_enabled", False)
        cv.row("webcmd", (inner.x, y, inner.w, s(110)), "Remote commands",
               sub="Start/cancel imports and change settings from the browser", toggle=en,
               action=lambda: app.set_setting({"web": {"commands_enabled": not en}}),
               icon="sliders")
        y += s(110)
        has_pin = bool(web.get("pin_hash"))
        cv.row("pin", (inner.x, y, inner.w, s(104)), "PIN", "set" if has_pin else "not set",
               lambda: app.push(TextInput("New PIN (4-8 digits)", "", lambda v: app.command(
                   "web.set_pin", lambda ok, r: app.push(Message(
                       "PIN" if ok else "PIN not changed", "The PIN has been changed." if ok
                       else str(r), "ok" if ok else "error")), pin=v), password=True,
                   numeric=True)), icon="lock")
        y += s(104)
        cv.row("port", (inner.x, y, inner.w, s(104)), "Port", str(port),
               lambda: app.push(TextInput("Web port", str(port), lambda v: app.set_setting(
                   {"web": {"port": int(v)}}) if v.isdigit() else None, numeric=True)),
               icon="network")

    def _draw_system(self, cv, inner):
        app, s, c = cv.app, cv.s, cv.c
        st = app.state or {}
        y = self._title(cv, inner, "System")
        cv.row("ver", (inner.x, y, inner.w, s(96)), "Version",
               f"{st.get('version', '')} (slot {st.get('slot', '?')})", None, chevron=False,
               icon="info")
        y += s(96)
        sl = self.slots
        if sl is None:
            if not getattr(self, "_slotreq", False):
                self._slotreq = True
                self._select(app, "system")
        other = None
        if sl and sl.get("slots"):
            cur = sl.get("current")
            other = "B" if cur == "A" else "A"
            o = sl["slots"].get(other, {})
            cv.row("other", (inner.x, y, inner.w, s(96)), "Previous version",
                   f"{o.get('version') or 'none'} (slot {other})", None, chevron=False,
                   icon="history")
            y += s(96)
            if sl.get("try_failed"):
                gfx.text(cv.surf, cv.t, f"An update was rolled back (slot {sl['try_failed']} "
                         "did not start correctly)", (inner.x + s(28), y + s(10)), "sm", c["warn"],
                         "medium", max_w=inner.w)
                y += s(60)
        cv.row("updates", (inner.x, y, inner.w, s(104)), "Updates", "coming soon", None,
               sub="From a USB stick, a web upload or the network", chevron=False,
               icon="update")
        y += s(104)
        by = inner.bottom - s(104)
        bw = (inner.w - 2 * s(24)) // 3
        cv.button("rollback", (inner.x, by, bw, s(104)), "Roll back",
                  lambda: app.confirm("Go back to the previous version?",
                                      f"The station restarts with the version in slot {other}.",
                                      "Roll back and restart",
                                      lambda: app.command("slots.rollback")),
                  "secondary", icon="history",
                  enabled=bool(other and sl["slots"].get(other, {}).get("present")))
        cv.button("reboot", (inner.x + bw + s(24), by, bw, s(104)), "Restart",
                  lambda: app.confirm("Restart the station?", "Running imports are cancelled.",
                                      "Restart", lambda: app.command("system.reboot")),
                  "secondary", icon="refresh")
        cv.button("poweroff", (inner.x + 2 * (bw + s(24)), by, bw, s(104)), "Shut down",
                  lambda: app.confirm("Shut down the station?", "Press the power button to "
                                      "switch it on again.", "Shut down",
                                      lambda: app.command("system.poweroff")),
                  "danger_ghost", icon="power")


class Wizard(Screen):
    """First boot: connect the network share, then show how to reach the
    station."""
    name = "wizard"
    default_focus = "next"

    def __init__(self, app):
        self.step = 0
        self.form = ShareForm(app.settings.get("share", {}))

    def on_nav(self, app, action):
        if action == "back":
            if self.step > 0:
                self.step -= 1
            return True
        return False

    def draw(self, cv: Canvas, rect: pygame.Rect) -> None:
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        box = pygame.Rect(0, 0, min(rect.w - s(120), s(1400)), min(rect.h - s(60), s(900)))
        box.center = rect.center
        gfx.card(cv.surf, box, t, s(40))
        inner = box.inflate(-s(120), -s(100))
        steps = ["Welcome", "Share", "Connect", "Folder", "Done"]
        # progress dots
        for i, name in enumerate(steps):
            x = inner.right - (len(steps) - i) * s(60)
            gfx.circle(cv.surf, (x, inner.y + s(20)), s(10),
                       c["accent"] if i <= self.step else c["track"])
        y = inner.y
        by = inner.bottom - s(110)
        if self.step == 0:
            gfx.text(cv.surf, t, "Welcome to Ingest Station", (inner.x, y + s(60)), "xxl",
                     c["text"], "bold")
            wrap(cv, "Let's connect the network share where the cards will be copied. You can "
                 "change everything later in Settings.", pygame.Rect(inner.x, y + s(200),
                                                                     inner.w, s(200)), "lg",
                 c["text2"], 3)
            ip = (app.state or {}).get("ip") or "waiting for the network…"
            gfx.text(cv.surf, t, f"This station: {ip}", (inner.x, y + s(420)), "md", c["text2"])
            cv.button("skip", (inner.x, by, s(300), s(110)), "Later",
                      lambda: app.finish_wizard(self), "ghost")
            cv.button("next", (inner.right - s(340), by, s(340), s(110)), "Start",
                      lambda: setattr(self, "step", 1), "primary", icon="chevron")
        elif self.step == 1:
            gfx.text(cv.surf, t, "Network share", (inner.x, y + s(40)), "xl", c["text"],
                     "semibold")
            self.form.rows(cv, inner.x, y + s(140), inner.w, prefix="w:")
            ok = bool(self.form.draft.get("server") and self.form.draft.get("path"))
            cv.button("next", (inner.right - s(340), by, s(340), s(110)), "Next",
                      lambda: (setattr(self, "step", 2), self.form.run_test(app)), "primary",
                      icon="chevron", enabled=ok)
            cv.button("prev", (inner.x, by, s(300), s(110)), "Back",
                      lambda: setattr(self, "step", 0), "ghost", icon="back")
        elif self.step == 2:
            gfx.text(cv.surf, t, "Connection test", (inner.x, y + s(40)), "xl", c["text"],
                     "semibold")
            d = self.form.draft
            gfx.text(cv.surf, t, f"{d.get('type', '').upper()} · {d.get('server')} · "
                     f"{d.get('path')}", (inner.x, y + s(130)), "md", c["text2"],
                     max_w=inner.w)
            self.form.test_line(cv, inner.x, y + s(220), inner.w)
            online = (self.form.test or {}).get("state") == "online"
            cv.button("retest", (inner.x + s(330), by, s(380), s(110)), "Test again",
                      lambda: self.form.run_test(app), "secondary", icon="refresh",
                      enabled=not self.form.testing)
            cv.button("prev", (inner.x, by, s(300), s(110)), "Back",
                      lambda: setattr(self, "step", 1), "ghost", icon="back")
            cv.button("next", (inner.right - s(340), by, s(340), s(110)), "Next",
                      lambda: self.form.save(app, lambda: setattr(self, "step", 3)), "primary",
                      icon="chevron", enabled=online)
        elif self.step == 3:
            gfx.text(cv.surf, t, "Destination folder", (inner.x, y + s(40)), "xl", c["text"],
                     "semibold")
            sub = self.form.draft.get("subdir") or ""
            wrap(cv, "Imports are copied into this folder of the share, one sub-folder per "
                 "import.", pygame.Rect(inner.x, y + s(130), inner.w, s(120)), "md", c["text2"], 2)
            cv.row("w:folder", (inner.x, y + s(260), inner.w, s(110)), "Folder",
                   "/" + sub if sub else "share root",
                   lambda: app.push(FolderBrowser(sub, lambda p: (
                       self.form.draft.__setitem__("subdir", p), self.form.save(app)))),
                   icon="folder")
            cv.button("prev", (inner.x, by, s(300), s(110)), "Back",
                      lambda: setattr(self, "step", 2), "ghost", icon="back")
            cv.button("next", (inner.right - s(340), by, s(340), s(110)), "Next",
                      lambda: setattr(self, "step", 4), "primary", icon="chevron")
        else:
            ip = (app.state or {}).get("ip") or "–"
            gfx.text(cv.surf, t, "All set", (inner.x, y + s(40)), "xxl", c["text"], "bold")
            wrap(cv, "Insert a card or connect a camera to start. From a phone or a computer "
                 "you can follow the imports at this address:",
                 pygame.Rect(inner.x, y + s(170), inner.w - s(420), s(200)), "lg", c["text2"], 3)
            gfx.text(cv.surf, t, f"http://{ip}", (inner.x, y + s(380)), "xl", c["accent"],
                     "semibold")
            gfx.text(cv.surf, t, "or http://ingest.local", (inner.x, y + s(470)), "md",
                     c["text2"])
            gfx.qr(cv.surf, f"http://{ip}", (inner.right - s(360), y + s(150), s(360), s(360)),
                   (17, 24, 39), (255, 255, 255))
            cv.button("next", (inner.right - s(340), by, s(340), s(110)), "Finish",
                      lambda: app.finish_wizard(self), "primary", icon="check")


class History(Screen):
    name = "history"
    default_focus = "back"

    def __init__(self):
        self.items = None
        self.offset = 0

    def on_nav(self, app, action):
        if action in ("pagedown", "down") and self.items and action == "pagedown":
            self.offset = min(self.offset + 6, max(0, len(self.items) - 6))
            return True
        if action == "pageup":
            self.offset = max(0, self.offset - 6)
            return True
        return super().on_nav(app, action)

    def draw(self, cv, rect):
        app, t, c, s = cv.app, cv.t, cv.c, cv.s
        if self.items is None and not getattr(self, "_req", False):
            self._req = True
            app.command("history", lambda ok, r: setattr(self, "items", r if ok else []),
                        limit=200)
        area = rect.inflate(-2 * s(56), -2 * s(36))
        cv.back("back", (area.x, area.y, s(220), s(88)), lambda: app.pop(self))
        gfx.text(cv.surf, t, "Import history", (area.x + s(270), area.y + s(44)), "xl",
                 c["text"], "semibold", "midleft")
        box = pygame.Rect(area.x, area.y + s(120), area.w, area.h - s(120))
        gfx.card(cv.surf, box, t, s(32))
        if self.items is None:
            gfx.text(cv.surf, t, "Loading…", box.center, "md", c["text2"], anchor="center")
            return
        if not self.items:
            gfx.text(cv.surf, t, "No imports yet", box.center, "lg", c["text2"], anchor="center")
            return
        per = 6
        rh = (box.h - s(60)) // per
        for i, imp in enumerate(self.items[self.offset:self.offset + per]):
            r = pygame.Rect(box.x + s(40), box.y + s(30) + i * rh, box.w - s(80), rh - s(8))
            d = dt.datetime.fromtimestamp(imp["started"])
            ok = imp["status"] == "done"
            col = c["ok"] if ok else c["err"] if imp["status"] == "failed" else c["warn"]
            gfx.circle(cv.surf, (r.x + s(16), r.centery), s(10), col)
            gfx.text(cv.surf, t, f"{d:%d %b %Y  %H:%M}", (r.x + s(50), r.centery), "md",
                     c["text"], "medium", "midleft")
            gfx.text(cv.surf, t, imp["dest_folder"], (r.x + s(420), r.centery), "md", c["text"],
                     anchor="midleft", max_w=r.w - s(900))
            gfx.text(cv.surf, t, f"{imp['files_done']} files · {human(imp['bytes_done'])}",
                     (r.right - s(200), r.centery), "sm", c["text2"], anchor="midright")
            gfx.text(cv.surf, t, imp["status"], (r.right, r.centery), "sm", col, "semibold",
                     "midright")
            if i < per - 1:
                pygame.draw.line(cv.surf, c["border"], (r.x, r.bottom + s(4)),
                                 (r.right, r.bottom + s(4)), s(2))
        if len(self.items) > per:
            gfx.text(cv.surf, t, f"{self.offset + 1}-{min(len(self.items), self.offset + per)} "
                     f"of {len(self.items)} · Page Up / Page Down", (box.right - s(40),
                                                                    box.bottom + s(10)),
                     "xs", c["text3"], anchor="topright")
