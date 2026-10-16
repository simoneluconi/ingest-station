"""ingestd: single source of truth of the station.

Watches the USB sources, mounts them read-only, scans them against the
index, runs one import at a time, monitors the network share and publishes
its state to local clients (GUI, web server, `ingestctl`) over a Unix
socket speaking JSON lines:

    request   {"id": 1, "cmd": "import", "args": {"source": "...", "mode": "new"}}
    reply     {"id": 1, "ok": true, "result": ...} | {"id": 1, "ok": false, "error": "..."}
    event     {"event": "state", "data": {...}}   (after {"cmd": "subscribe"})

It keeps running whatever happens to its clients.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

from ingest import __version__
from ingest.common import config, sysfs

from . import devices, mounts, naming, ptp, rebuild, scanner, share
from .beeper import Beeper
from .copier import Cancelled, Item
from .importer import ImportJob
from .index import Index

SOCKET = "/run/ingestd.sock"
RESCAN_EVERY = 2.0
SHARE_EVERY = 30.0
IP_EVERY = 5.0
PUBLISH_EVERY = 0.25

log = logging.getLogger("ingestd")


@dataclass
class Source:
    id: str
    slot: str                      # card reader slot / USB port / camera identity
    disk: devices.Disk | None = None
    volume: devices.Volume | None = None
    kind: str = "disk"             # disk | camera
    cam: dict | None = None        # camera: model, vendor, serial, storage (ptp.Storage), n
    status: str = "detected"       # empty detected mounting scanning ready countdown
                                   # queued importing done error unsupported update installer
    message: str = ""
    mount: str | None = None
    scan: scanner.ScanResult | None = None
    stale: bool = False            # imported since the scan: rescan before importing again
    countdown: int | None = None
    result: dict | None = None
    request: dict = field(default_factory=dict)   # mode / verify chosen for the import
    cancel: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None

    def info(self) -> dict:
        if self.kind == "camera" and self.cam:
            st = self.cam.get("storage")
            name = self.cam["model"]
            if st is not None and self.cam.get("count", 1) > 1:
                name += f" - {st.description or st.label or 'slot ' + str(self.cam['n'] + 1)}"
            speed, slow = devices.usb_link(self.cam.get("sysdir"))
            return {"name": name, "model": self.cam["model"], "vendor": self.cam["vendor"],
                    "serial": self.cam["serial"], "capacity": st.capacity if st else 0,
                    "fs": "camera", "label": "", "usb_speed": speed, "usb_slow": slow,
                    "phone": bool(self.cam.get("phone"))}
        d = self.disk
        return {"name": devices.describe(d) if d else "", "model": d.model if d else "",
                "vendor": d.vendor if d else "", "serial": d.serial if d else "",
                "capacity": d.size if d else 0,
                "fs": self.volume.fstype if self.volume else "",
                "label": self.volume.label if self.volume else "",
                "usb_speed": d.usb_speed if d else 0, "usb_slow": d.usb_slow if d else False}

    def to_dict(self) -> dict:
        d = {"id": self.id, "slot": self.slot, "kind": self.kind, "status": self.status,
             "message": self.message, **self.info(),
             "countdown": self.countdown, "result": self.result}
        if self.scan is not None:
            d["summary"] = self.scan.summary()
        return d


def _remove_hint(src: Source) -> str:
    return "You can disconnect the camera" if src.kind == "camera" else "You can remove the card"


def slot_key(disk: devices.Disk) -> str:
    return f"{disk.usb_port or disk.name}:{disk.lun or '0'}"


class Daemon:
    def __init__(self):
        self.settings = config.load()
        self.index = Index(config.INDEX_DB)
        devices.SS_PORTS = devices.SuperSpeedPorts(
            os.path.join(config.CONFIG_DIR, "usb3-ports")
            if os.path.isdir(config.CONFIG_DIR) else None)
        self.beeper = Beeper(self.settings["beep"]["backend"])
        self.sources: dict[str, Source] = {}
        self.queue: list[str] = []
        self.active: dict | None = None     # progress of the running import
        self.active_id: str | None = None
        self.pending_decision: dict | None = None   # {"error", "event", "choice"}
        self.last_result: dict | None = None        # outcome of the last import, kept
                                                    # after the card is removed
        self.share_state: dict = {"state": "unknown", "reason": "checking..."}
        self.subscribers: set[asyncio.StreamWriter] = set()
        self.ip = ""
        self._dirty = asyncio.Event()
        self._rescan = asyncio.Event()
        self._share_now = asyncio.Event()
        self._disk_sig: dict[str, tuple] = {}
        self._cam_sig: dict[str, tuple] = {}
        self._no_auto: set[str] = set()     # slots rescanned after a cancelled import
        self.cameras: dict[str, object] = {}        # key -> ptp.Camera
        self.loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        s = self.settings
        return {
            "version": __version__, "time": time.time(), "ip": self.ip,
            "slot": sysfs.kernel_cmdline().get("ingest_slot", ""),
            "share": {**self.share_state, "type": s["share"]["type"],
                      "server": s["share"]["server"], "path": s["share"]["path"],
                      "subdir": s["share"]["subdir"]},
            "sources": [self.sources[k].to_dict() for k in sorted(self.sources)],
            "queue": list(self.queue),
            "active": ({**self.active, "source": self.active_id} if self.active else None),
            "decision": ({"error": self.pending_decision["error"]}
                         if self.pending_decision else None),
            "last_result": self.last_result,
            "settings": {k: v for k, v in s.items() if k != "share"},
        }

    def changed(self) -> None:
        self._dirty.set()

    async def publisher(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            msg = (json.dumps({"event": "state", "data": self.state()}) + "\n").encode()
            for w in list(self.subscribers):
                try:
                    w.write(msg)
                    await asyncio.wait_for(w.drain(), 2)
                except Exception:  # slow or gone client
                    self.subscribers.discard(w)
            await asyncio.sleep(PUBLISH_EVERY)

    # ---------------------------------------------------------------- devices
    async def uevents(self) -> None:
        sock = devices.uevent_socket()
        if sock is None:
            return
        loop = asyncio.get_running_loop()

        def on_readable():
            try:
                while True:
                    ev = devices.parse_uevent(sock.recv(65536))
                    if ev.get("SUBSYSTEM") in ("block", "usb", "scsi"):
                        self._rescan.set()
            except BlockingIOError:
                pass
            except OSError:
                pass
        loop.add_reader(sock.fileno(), on_readable)

    async def rescanner(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._rescan.wait(), RESCAN_EVERY)
                await asyncio.sleep(0.3)   # let a burst of events settle
            except asyncio.TimeoutError:
                pass
            self._rescan.clear()
            try:
                await self.refresh()
            except Exception:
                log.exception("device refresh failed")

    async def refresh(self) -> None:
        disks = await asyncio.to_thread(devices.scan_disks)
        seen_slots = set()
        for disk in disks:
            key = slot_key(disk)
            seen_slots.add(key)
            sig = (disk.name, disk.size, tuple(disk.partitions))
            if self._disk_sig.get(key) == sig:
                continue
            self._disk_sig[key] = sig
            await self._slot_changed(key, disk)
        for key in [k for k in self._disk_sig if k not in seen_slots]:
            del self._disk_sig[key]
            await self._drop_slot(key, removed=True)
        cams = await asyncio.to_thread(ptp.usb_cameras)
        seen_cams = set()
        for ucam in cams:
            key = f"cam:{ucam.serial or ucam.port}"
            seen_cams.add(key)
            sig = (ucam.busnum, ucam.devnum)
            if self._cam_sig.get(key) == sig:
                continue
            self._cam_sig[key] = sig
            await self._drop_slot(key, removed=False)
            asyncio.create_task(self._camera_added(key, ucam))
        for key in [k for k in self._cam_sig if k not in seen_cams]:
            del self._cam_sig[key]
            await self._drop_slot(key, removed=True)

    async def _slot_changed(self, key: str, disk: devices.Disk) -> None:
        await self._drop_slot(key, removed=False)
        log.info("slot %s: %s, %d bytes, partitions %s", key, disk.name, disk.size,
                 ",".join(disk.partitions) or "none")
        if not disk.has_media:
            self.sources[key] = Source(id=key, slot=key, disk=disk, status="empty",
                                       message="No card")
            self.changed()
            return
        vols = await asyncio.to_thread(devices.volumes, disk)
        log.info("slot %s: volumes %s", key,
                 ", ".join(f"{v.dev} ({v.fstype or '?'})" for v in vols) or "none")
        if not vols:
            self.sources[key] = Source(id=key, slot=key, disk=disk, status="unsupported",
                                       message="No readable file system")
            self.changed()
            return
        for n, vol in enumerate(vols):
            sid = key if len(vols) == 1 else f"{key}:{vol.dev.rsplit('/', 1)[-1]}"
            src = Source(id=sid, slot=key, disk=disk, volume=vol)
            self.sources[sid] = src
            if devices.mount_type(vol.fstype) is None:
                src.status, src.message = "unsupported", devices.unsupported_reason(vol.fstype)
                continue
            src.task = asyncio.create_task(self._prepare(src))
        self.beeper.play("start")
        self.changed()

    async def _drop_slot(self, key: str, removed: bool) -> None:
        if removed:
            self._no_auto.discard(key)
            log.info("slot %s: removed", key)
        for sid in [s for s, src in self.sources.items() if src.slot == key]:
            src = self.sources.pop(sid)
            src.cancel.set()
            if sid in self.queue:
                self.queue.remove(sid)
            if self.active_id == sid and self.pending_decision:
                self._decide("cancel")
            if src.task and not src.task.done() and sid != self.active_id:
                src.task.cancel()
            if src.mount:
                await asyncio.to_thread(mounts.umount, src.mount, True)
        cam = self.cameras.pop(key, None)
        if cam is not None:
            await asyncio.to_thread(_close_quietly, cam)
        self.changed()

    async def _prepare(self, src: Source) -> None:
        """Mount read-only and scan against the index."""
        try:
            src.status = "mounting"
            self.changed()
            src.mount = await asyncio.to_thread(
                mounts.mount_ro, src.volume.dev, devices.mount_type(src.volume.fstype),
                src.id.replace(":", "_").replace("/", "_"))
            if await asyncio.to_thread(devices.is_update_media, src.mount):
                src.status, src.message = "update", "Update package found"
                self.changed()
                return
            if os.path.isfile(os.path.join(src.mount, "slots", "A", "slot.json")):
                src.status, src.message = "installer", "Ingest Station installation stick"
                self.changed()
                return
            src.status = "scanning"
            self.changed()
            t0 = time.monotonic()
            src.scan = await asyncio.to_thread(scanner.scan, src.mount, self.index, src.cancel)
            src.status = "ready"
            src.message = ""
            log.info("source %s ready: %d files, %d new (scan %.1f s)", src.id,
                     len(src.scan.files), src.scan.new_files, time.monotonic() - t0)
            self.changed()
            await self._maybe_auto_import(src)
        except asyncio.CancelledError:
            raise
        except Cancelled:
            pass
        except Exception as e:
            src.status, src.message = "error", str(e) or e.__class__.__name__
            log.exception("prepare %s", src.id)
            self.beeper.play("error")
            self.changed()

    # ---------------------------------------------------------------- cameras
    async def _camera_added(self, key: str, ucam) -> None:
        """Open a PTP/MTP device and create one source per storage."""
        placeholder = Source(id=key, slot=key, kind="camera", status="detected",
                             message="Connecting to the camera...",
                             cam={"model": ucam.product or "Camera", "vendor": ucam.vendor,
                                  "serial": ucam.serial, "storage": None, "n": 0, "count": 1,
                                  "sysdir": ucam.sysdir})
        self.sources[key] = placeholder
        self.changed()
        try:
            models = await asyncio.to_thread(ptp.autodetect)
            model = models.get(ucam.port)
            if not model:
                raise ptp.PTPError("camera not recognised: unlock it or allow file access "
                                   "(phones: USB mode 'File transfer')")
            cam = await asyncio.to_thread(ptp.Camera, model, ucam.port)
            self.cameras[key] = cam
            storages = await asyncio.to_thread(cam.storages)
            name = ptp.device_name(model, await asyncio.to_thread(cam.summary))
            log.info("camera %s: %s (libgphoto2: %s), %d storage(s)", key, name, model,
                     len(storages))
        except Exception as e:
            placeholder.status, placeholder.message = "error", str(e)
            # try again on the next rescan (the user may unlock a phone)
            self._cam_sig.pop(key, None)
            self.changed()
            return
        if self.sources.get(key) is placeholder:
            del self.sources[key]
        for n, st in enumerate(storages):
            sid = f"{key}:{n}"
            src = Source(id=sid, slot=key, kind="camera",
                         cam={"model": name, "vendor": ucam.vendor, "serial": ucam.serial,
                              "storage": st, "n": n, "count": len(storages),
                              "sysdir": ucam.sysdir, "phone": ptp.is_phone(model)})
            self.sources[sid] = src
            src.task = asyncio.create_task(self._prepare_camera(src, cam))
        self.beeper.play("start")
        self.changed()

    async def _prepare_camera(self, src: Source, cam) -> None:
        try:
            src.status = "scanning"
            self.changed()
            st = src.cam["storage"]
            t0 = time.monotonic()
            files = await asyncio.to_thread(cam.walk, st.basedir, src.cancel)
            base = st.basedir.rstrip("/")
            items = []
            for cf in files:
                rel = f"{cf.folder[len(base):]}/{cf.name}".lstrip("/")
                items.append(Item(src=f"{cf.folder}/{cf.name}",
                                  rel=naming.sanitize_relpath(rel), size=cf.size,
                                  mtime=cf.mtime,
                                  opener=functools.partial(ptp.CameraStream, cam, cf)))
            dcim = sorted({i.rel.split("/")[1] for i in items
                           if i.rel.upper().startswith("DCIM/") and i.rel.count("/") >= 2})
            src.scan = await asyncio.to_thread(scanner.scan_items, items, self.index,
                                               src.cancel, naming.dcim_brand(dcim))
            src.status, src.message = "ready", ""
            log.info("source %s ready: %d files, %d new (scan %.1f s)", src.id,
                     len(src.scan.files), src.scan.new_files, time.monotonic() - t0)
            self.changed()
            await self._maybe_auto_import(src)
        except asyncio.CancelledError:
            raise
        except Cancelled:
            pass
        except Exception as e:
            src.status, src.message = "error", str(e) or e.__class__.__name__
            log.exception("camera %s", src.id)
            self.beeper.play("error")
            self.changed()

    async def _maybe_auto_import(self, src: Source) -> None:
        if src.slot in self._no_auto:
            self._no_auto.discard(src.slot)
            return
        if not self.settings["auto_import"] or not src.scan or src.scan.new_files == 0:
            return
        src.status = "countdown"
        src.request = {"mode": "new", "verify": self.settings["verify_default"]}
        for left in range(int(self.settings["auto_import_delay"]), 0, -1):
            if src.status != "countdown":
                return
            src.countdown = left
            self.changed()
            await asyncio.sleep(1)
        if src.status == "countdown":
            src.countdown = None
            self._enqueue(src)

    # ----------------------------------------------------------------- imports
    def _enqueue(self, src: Source) -> None:
        src.status = "queued"
        if src.id not in self.queue:
            self.queue.append(src.id)
        self.changed()
        asyncio.get_running_loop().call_soon(self._kick)

    def _kick(self) -> None:
        if self.active_id is None and self.queue:
            sid = self.queue.pop(0)
            src = self.sources.get(sid)
            if src:
                asyncio.create_task(self._run_import(src))

    async def _run_import(self, src: Source) -> None:
        self.active_id = src.id
        src.status = "importing"
        src.cancel.clear()
        self.active = {"phase": "starting", "percent": 0.0}
        self.changed()
        self.beeper.play("start")
        loop = asyncio.get_running_loop()

        def report(progress: dict) -> None:
            loop.call_soon_threadsafe(self._progress, progress)

        def decide(error: str) -> str:
            ev = threading.Event()
            holder = {"error": error, "event": ev, "choice": "cancel"}
            loop.call_soon_threadsafe(self._set_decision, holder)
            ev.wait()
            loop.call_soon_threadsafe(self._set_decision, None)
            return holder["choice"]

        def ensure_share() -> None:
            st = share.check(self.settings["share"])
            loop.call_soon_threadsafe(self._set_share, st)
            if st["state"] != "online":
                raise share.ShareError(st.get("reason") or "share offline")

        req = src.request
        job = ImportJob(
            items=src.scan.plan(req.get("mode", "new")), verify=req.get("verify", "hash"),
            share_root=share.dest_root(self.settings["share"]), index=self.index,
            source={**src.info(), "kind": src.kind, "brand": src.scan.brand},
            schema=self.settings["folder_schema"], report=report, decide=decide,
            ensure_share=ensure_share, cancel=src.cancel,
            verify_mbps=self.settings["stats"]["verify_mbps"])
        try:
            res = await asyncio.to_thread(job.run)
            src.result = {"status": res.status, "folder": res.folder, "error": res.error,
                          "files": res.files, "bytes": res.bytes, "seconds": round(res.seconds),
                          "mbps": round(res.copy_mbps, 1)}
            src.status = "done" if res.status == "done" else "error"
            src.message = (_remove_hint(src) if res.status == "done" else res.error)
            self.last_result = {**src.result, "source": src.id,
                                "name": src.info()["name"], "time": time.time()}
            if res.status == "done" and res.copy_mbps > 1:
                st = self.settings["stats"]
                st["copy_mbps"] = round(0.7 * st["copy_mbps"] + 0.3 * res.copy_mbps, 1)
                if res.verify_mbps > 1:
                    st["verify_mbps"] = round(0.7 * st["verify_mbps"] + 0.3 * res.verify_mbps, 1)
                await asyncio.to_thread(config.update, {"stats": st})
            self.beeper.play("ok" if res.status == "done" else "error")
        except Exception as e:
            log.exception("import %s", src.id)
            src.status, src.message = "error", str(e)
            self.beeper.play("error")
        finally:
            self.active, self.active_id = None, None
            src.stale = True
            if src.mount and src.id in self.sources:
                # release the card: it can be removed safely
                await asyncio.to_thread(mounts.umount, src.mount)
                src.mount = None
            if src.status != "done" and self.sources.get(src.id) is src:
                # cancelled or failed: scan again, so that the files copied so
                # far are recognised; no automatic import right after that
                self._no_auto.add(src.slot)
                self._disk_sig.pop(src.slot, None)
                self._cam_sig.pop(src.slot, None)
                self._rescan.set()
            self.changed()
            self._kick()

    def _progress(self, progress: dict) -> None:
        if self.active is not None:
            self.active = progress
            self.changed()

    def _set_decision(self, holder: dict | None) -> None:
        self.pending_decision = holder
        if holder:
            self.beeper.play("error")
            if "share" in holder["error"]:
                self.share_state = {**self.share_state, "state": "offline",
                                    "reason": holder["error"]}
        self.changed()

    def _decide(self, choice: str) -> None:
        holder = self.pending_decision
        if holder:
            holder["choice"] = choice
            holder["event"].set()

    def _set_share(self, st: dict) -> None:
        self.share_state = st
        self.changed()

    async def share_monitor(self) -> None:
        """Re-check the share every 30 s when online, every 5 s when not,
        and at once when the network address changes."""
        first = True
        while True:
            if self.active_id is None:
                st = await asyncio.to_thread(share.check, self.settings["share"],
                                             write=first)
                self._set_share(st)
                first = st["state"] != "online"
            delay = SHARE_EVERY if not first else IP_EVERY
            try:
                await asyncio.wait_for(self._share_now.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._share_now.clear()

    async def ip_monitor(self) -> None:
        while True:
            ip = await asyncio.to_thread(local_ip)
            if ip != self.ip:
                self.ip = ip
                self._share_now.set()
                self.changed()
            await asyncio.sleep(IP_EVERY)

    # --------------------------------------------------------------- commands
    async def command(self, cmd: str, args: dict):
        if cmd == "ping":
            return "pong"
        if cmd == "state":
            return self.state()
        if cmd == "import":
            return await self._cmd_import(args)
        if cmd == "cancel":
            src = self._source(args)
            if src.status == "countdown":
                src.status, src.countdown = "ready", None
            elif src.id in self.queue:
                self.queue.remove(src.id)
                src.status = "ready"
            elif src.id == self.active_id:
                src.cancel.set()
                self._decide("cancel")
            self.changed()
            return True
        if cmd == "dismiss":
            self.last_result = None
            self.changed()
            return True
        if cmd in ("retry", "decide"):
            if not self.pending_decision:
                raise ValueError("nothing to retry")
            self._decide(args.get("choice", "retry") if cmd == "decide" else "retry")
            return True
        if cmd == "eject":
            src = self._source(args)
            if src.id == self.active_id:
                raise ValueError("an import is running on this source: cancel it first")
            if src.mount:
                await asyncio.to_thread(mounts.umount, src.mount)
                src.mount = None
            src.status, src.message = "ejected", _remove_hint(src)
            self.changed()
            return True
        if cmd == "rescan":
            src = self._source(args)
            if src.id == self.active_id:
                raise ValueError("import running")
            self._disk_sig.pop(src.slot, None)
            self._cam_sig.pop(src.slot, None)
            self._rescan.set()
            return True
        if cmd == "settings.get":
            return self.settings
        if cmd == "settings.set":
            return await self._cmd_settings(args)
        if cmd == "share.test":
            cfg = {**self.settings["share"], **args.get("share", {})}
            return await asyncio.to_thread(share.test, cfg, args.get("password"))
        if cmd == "share.list":
            return await asyncio.to_thread(share.list_dirs, self.settings["share"],
                                           args.get("path", ""))
        if cmd == "share.mkdir":
            await asyncio.to_thread(share.make_dir, self.settings["share"], args["path"])
            return True
        if cmd == "history":
            return await asyncio.to_thread(self.index.history, int(args.get("limit", 50)),
                                           args.get("query", ""))
        if cmd in ("system.reboot", "system.poweroff"):
            prog = "reboot" if cmd == "system.reboot" else "poweroff"
            asyncio.get_running_loop().call_later(1.5, lambda: subprocess.Popen([prog]))
            return True
        if cmd == "slots.status":
            return await asyncio.to_thread(_slots_status)
        if cmd == "slots.rollback":
            if self.active_id:
                raise ValueError("an import is running")
            await asyncio.to_thread(_slots_rollback)
            asyncio.get_running_loop().call_later(1.5, lambda: subprocess.Popen(["reboot"]))
            return True
        if cmd == "network.get":
            return await asyncio.to_thread(config.read_network)
        if cmd == "network.set":
            net = await asyncio.to_thread(config.write_network, args)
            subprocess.Popen(["rc-service", "ingest-network", "restart"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return net
        if cmd == "web.set_pin":
            pin = str(args.get("pin", ""))
            if not (pin.isdigit() and 4 <= len(pin) <= 8):
                raise ValueError("the PIN must have 4 to 8 digits")
            self.settings = await asyncio.to_thread(
                config.update, {"web": {"pin_hash": config.pin_hash(pin)}})
            self.changed()
            return True
        if cmd == "index.rebuild":
            # full=True: forget everything first, so folders deleted from the
            # share no longer count as imported
            if self.active_id or self.queue:
                raise ValueError("an import is running")
            st = await asyncio.to_thread(share.check, self.settings["share"])
            if st["state"] != "online":
                raise ValueError(f"share offline: {st.get('reason', '')}")
            if args.get("full"):
                await asyncio.to_thread(self.index.reset)
            res = await asyncio.to_thread(rebuild.rebuild, self.index,
                                          share.dest_root(self.settings["share"]))
            log.info("index rebuilt from the share: %s", res)
            self._rescan_all()
            return res
        if cmd == "index.reset":
            if self.active_id or self.queue:
                raise ValueError("an import is running")
            n = await asyncio.to_thread(self.index.reset)
            log.info("index reset: %d imports forgotten", n)
            self.last_result = None
            self._rescan_all()
            return {"imports": n}
        if cmd == "beep":
            Beeper(args.get("backend", self.settings["beep"]["backend"])).play(
                args.get("pattern", "ok"))
            return self.beeper.available()
        raise ValueError(f"unknown command {cmd!r}")

    def _rescan_all(self) -> None:
        """Scan every source again (after the index changed)."""
        self._disk_sig.clear()
        self._cam_sig.clear()
        self._no_auto.update(src.slot for src in self.sources.values())
        self._rescan.set()

    def _source(self, args: dict) -> Source:
        sid = args.get("source", "")
        if sid not in self.sources:
            # accept a unique prefix / the only source
            matches = [s for s in self.sources if s.startswith(sid)] if sid else list(
                s for s, v in self.sources.items() if v.status != "empty")
            if len(matches) != 1:
                raise ValueError(f"unknown source {sid!r}")
            sid = matches[0]
        return self.sources[sid]

    async def _cmd_import(self, args: dict):
        src = self._source(args)
        if src.status not in ("ready", "countdown", "done", "error"):
            raise ValueError(f"source is {src.status}, not ready")
        if src.scan is None:
            raise ValueError("source not scanned")
        mode = args.get("mode", "new")
        verify = args.get("verify", self.settings["verify_default"])
        if mode not in ("new", "all") or verify not in config.VERIFY_MODES:
            raise ValueError("invalid mode or verification")
        if not src.scan.plan(mode):
            raise ValueError("nothing to import: every file was already imported")
        st = await asyncio.to_thread(share.check, self.settings["share"])
        self._set_share(st)
        if st["state"] != "online":
            raise ValueError(f"share offline: {st.get('reason', '')}")
        if src.stale or (src.kind == "disk" and src.mount is None):
            raise ValueError("already imported from this scan: rescan the source first")
        src.request = {"mode": mode, "verify": verify}
        src.countdown = None
        self._enqueue(src)
        return {"queued": src.id, "position": self.queue.index(src.id) if src.id in self.queue else 0}

    async def _cmd_settings(self, args: dict):
        changes = dict(args.get("changes", {}))
        password = args.get("smb_password")
        old_share = dict(self.settings["share"])
        new = await asyncio.to_thread(config.update, changes)
        if password is not None:
            await asyncio.to_thread(config.write_smb_credentials,
                                    new["share"].get("username", ""), password,
                                    new["share"].get("domain", ""))
        self.settings = new
        self.beeper.backend = new["beep"]["backend"]
        if new["share"] != old_share or password is not None:
            if self.active_id:
                raise ValueError("saved; the share will be re-mounted after the running import")
            await asyncio.to_thread(share.umount)
            self._set_share(await asyncio.to_thread(share.check, new["share"]))
        self.changed()
        return new

    # --------------------------------------------------------------------- api
    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                if req.get("cmd") == "subscribe":
                    self.subscribers.add(writer)
                    writer.write((json.dumps({"event": "state", "data": self.state()})
                                  + "\n").encode())
                    await writer.drain()
                    continue
                try:
                    result = await self.command(req.get("cmd", ""), req.get("args") or {})
                    resp = {"id": req.get("id"), "ok": True, "result": result}
                except Exception as e:  # noqa: BLE001 - reported to the client
                    resp = {"id": req.get("id"), "ok": False, "error": str(e)}
                writer.write((json.dumps(resp, default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.subscribers.discard(writer)
            writer.close()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        n = self.index.mark_interrupted()
        if n:
            log.warning("%d import(s) were interrupted", n)
        await asyncio.to_thread(mounts.cleanup_stale)
        # a share left mounted by a previous run is mounted again, so that the
        # protocol version is negotiated (and checked) with the current code
        await asyncio.to_thread(share.umount, share.MOUNT)
        await asyncio.to_thread(share.umount, share.TEST_MOUNT)
        if os.path.exists(SOCKET):
            os.remove(SOCKET)
        server = await asyncio.start_unix_server(self.client, path=SOCKET)
        os.chmod(SOCKET, 0o660)
        await self.uevents()
        await asyncio.gather(server.serve_forever(), self.publisher(), self.rescanner(),
                             self.share_monitor(), self.ip_monitor())


def _slots_status() -> dict:
    from ingest.common import slots
    esp = slots.find_esp()
    if not esp:
        return {}
    return slots.SlotManager(esp, sysfs.kernel_cmdline().get("ingest_slot")).status()


def _slots_rollback() -> None:
    from ingest.common import slots
    esp = slots.find_esp()
    if not esp:
        raise ValueError("not running from the eMMC")
    mgr = slots.SlotManager(esp, sysfs.kernel_cmdline().get("ingest_slot"))
    mgr.set_active(slots.other(mgr.env().get("active_slot", "A")))


def _close_quietly(cam) -> None:
    try:
        cam.close()
    except Exception:  # noqa: BLE001 - the camera may be gone already
        pass


def local_ip() -> str:
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, timeout=5).stdout
        for line in sysfs.decode(out).splitlines():
            f = line.split()
            if "inet" in f:
                return f[f.index("inet") + 1].split("/")[0]
    except Exception:
        pass
    return ""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=os.path.join(config.LOG_DIR, "ingestd.log")
                        if os.path.isdir(config.LOG_DIR) else None)
    socket.setdefaulttimeout(None)
    asyncio.run(Daemon().run())
    return 0
