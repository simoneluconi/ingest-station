# GUI

`ingest-gui` is the full-screen interface on the station's monitor. It is a
client of `ingestd`: it shows the daemon's state and sends it commands, and
it never touches devices or the share itself. If it crashes, imports go on
and `supervise-daemon` restarts it.

## How it draws

- **No X, no Mesa.** pygame draws into an off-screen 32-bit surface; every
  frame is copied into a KMS/DRM dumb buffer (`drm.py`, pure-Python ioctls),
  with an fbdev fallback. Monitors can be plugged in and out at any time.
- **Immediate mode.** Every frame, each screen draws itself and registers
  its interactive widgets (`ui.Canvas`), each with a stable key. The app
  keeps the focused key per screen, moves it with spatial navigation
  (`ui.move_focus`), and activates widgets on Enter, click or tap. The focus
  ring is always visible.
- **Input** comes straight from evdev (`inputdev.py`): keyboards (Italian
  and US layouts), mice, touchscreens, numeric keypads. Devices are grabbed
  so keystrokes never reach the console.
- **Themes** are colour tokens (`theme.py`: light and dark) and sizes are
  given on a 1080p grid and scaled (`Theme.s`).
- **Drawing primitives** (`gfx.py`) add what pygame lacks: anti-aliased
  rounded rectangles (supersampled corners), soft shadows, vector icons on a
  24-unit grid, cached text, QR codes, a PNG writer for screenshots.

| Module | Content |
|---|---|
| `app.py` | main loop, daemon connection (listener thread), screen stack, automatic navigation, status bar, focus ring, heartbeat, screenshots |
| `screens.py` | home, source summary, progress, pause, result; shared helpers (`status_block`, `wrap`, `link_speed`) |
| `settings.py` | settings categories, first-boot wizard, history, `ShareForm` |
| `dialogs.py` | confirm, message, choice list, text input with on-screen keyboard, folder browser |
| `ui.py` | widgets: buttons, rows, segmented controls, chips, back item |
| `gfx.py`, `theme.py`, `drm.py`, `inputdev.py` | drawing, colours, display, input |

## Layout rules

These keep the screens consistent; follow them when adding one.

- **Alignment columns.** In a panel, the title, section labels and row icons
  share one left edge (`inner.x + 28`); row labels follow the icon. Page
  headers put "‹ Back" on the categories' columns and the title on the
  panel's.
- **Text anchors.** Fonts are drawn with their full line box (`pad=True`),
  so `midleft`/`center` anchors line up whatever the glyphs. Texts of
  different sizes on one line use the `baseline` anchors (`"0 files"`).
- **Long texts** get `max_w` and, where it matters, `min_size`: they shrink
  first and are ellipsized only below that size.
- **Outcomes** (done, nothing new, ejected, errors, connection test) use
  `status_block`: icon in a soft circle, title in the tone's colour, a
  detail line and a quieter hint line. Tones: `ok` green, `warn` amber
  (cancelled, slow link), `err` red, `accent` blue.
- **Dialogs** are centred: icon (messages), title, text, then the buttons;
  in confirmations Cancel is on the left and focused by default.
- **Buttons**: one `primary` action per screen; secondary actions are
  outlined (`secondary`); destructive ones are `danger` (or `danger_ghost`
  when they are not the main action).
- **Section labels** are small uppercase `text3` ("DEFAULT VERIFICATION").
- **Anti-aliasing**: a transparent surface that is drawn on and then scaled
  must be pre-filled with the drawing colour at alpha 0
  (`gfx._clear_surface`); the default transparent black darkens every edge.

## Developing on the station

There is no simulation on the PC: the GUI is tested on the Wyse.

```sh
python tools/wyse.py push ingest.local --restart ingest-gui   # code into RAM, restart
sh tools/shot.sh work/home.png                                # screenshot
sh tools/shot.sh work/s.png screen:settings cat:system        # navigate, then screenshot
```

`push` copies the code into the running system only: a reboot restores the
installed version (`build.py --deploy` installs it for good). After a
restart, wait until `/run/ingest-gui.alive` is recreated before sending
commands: the signal used by `shot.sh` would stop a GUI that has not set up
its handlers yet.

`shot.sh` writes commands to `/tmp/ingest-gui.cmd`, signals the GUI
(`SIGUSR2`), then asks for a screenshot (`SIGUSR1`, saved as
`/tmp/ingest-gui.png`) and downloads it. Commands, one per argument:

| Command | Effect |
|---|---|
| `up` `down` `left` `right` `ok` `back` | navigation, as with the keyboard |
| `focus:KEY` | focus a widget by key (e.g. `focus:import_new`, `focus:tile:<source id>`) |
| `screen:settings`, `screen:history`, `screen:wizard` | open a screen |
| `cat:CATEGORY` | settings category (`dest`, `import`, `display`, `network`, `web`, `system`) |
| `msg:error`, `msg:ok`, `msg:info` | show a sample message dialog |
| `offline` | show the share as offline until the next state update |

A dialog's default focus is set when it is first drawn: open it in one
`shot.sh` call and press its buttons (`focus:yes ok`) in the next.

Logs: GUI tracebacks in `/var/log/ingest-gui.log`, the daemon in
`/data/log/ingestd.log`, boot confirmations in `/data/log/boot.log`.
