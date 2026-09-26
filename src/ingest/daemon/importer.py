"""One import job: copy (and verify) a plan of files into a new folder on
the share. Runs in a worker thread; talks to the daemon through callbacks.

- The destination folder is AAAA-MM-GG_HHMM_<label> (schema configurable),
  with the original folder structure inside.
- Every completed file is recorded in the index at once, so an interrupted
  import resumes from the last completed file.
- A destination error pauses the job: `decide(error)` blocks until the user
  chooses "retry" (restart from the failed file, rewritten from scratch) or
  "cancel". There are no automatic retries.
- Verification: none | hash (xxh128 while copying, manifest saved) | full
  (hash, then the copies are read back from the share and compared).
"""

from __future__ import annotations

import collections
import datetime as dt
import os
import threading
import time
from dataclasses import dataclass, field

from ingest import __version__

from . import copier, naming
from .copier import Cancelled, DestError, Item, SourceError
from .index import Index

REPORT_EVERY = 0.25


@dataclass
class JobResult:
    status: str                     # done | failed | cancelled
    folder: str = ""
    error: str = ""
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    copy_mbps: float = 0.0
    verify_mbps: float = 0.0
    mismatches: list[str] = field(default_factory=list)


class Meter:
    """Instantaneous (last ~3 s) and average throughput."""

    def __init__(self):
        self.start = time.monotonic()
        self.samples: collections.deque = collections.deque()
        self.total = 0

    def add(self, n: int) -> None:
        now = time.monotonic()
        self.total += n
        self.samples.append((now, self.total))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.popleft()

    def now_bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def avg_bps(self) -> float:
        dt_ = time.monotonic() - self.start
        return self.total / dt_ if dt_ > 0.5 else 0.0


class ImportJob:
    def __init__(self, *, items: list[Item], verify: str, share_root: str, index: Index,
                 source: dict, schema: str, report, decide, ensure_share,
                 cancel: threading.Event, verify_mbps: float = 90.0):
        self.items = items
        self.verify = verify
        self.share_root = share_root
        self.index = index
        self.source = source          # label, model, serial, kind, name
        self.schema = schema
        self.report = report          # report(dict) -> None
        self.decide = decide          # decide(error: str) -> "retry" | "cancel"
        self.ensure_share = ensure_share
        self.cancel = cancel
        self.verify_mbps = verify_mbps
        self.log: list[str] = []
        self.folder = ""
        self.import_id = 0
        self.total_bytes = sum(i.size for i in items)
        self.done_bytes = 0
        self.done_files = 0
        self.phase = "copy"
        self.current = ""
        self.meter = Meter()
        self._last_report = 0.0

    # -- reporting ---------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    def _progress(self, n: int) -> None:
        self.done_bytes += n
        self.meter.add(n)
        now = time.monotonic()
        if now - self._last_report >= REPORT_EVERY:
            self._last_report = now
            self._emit()

    def _emit(self, **extra) -> None:
        avg = self.meter.avg_bps()
        remaining = max(self.total_bytes - self.done_bytes, 0) if self.phase == "copy" else 0
        eta = remaining / avg if avg > 0 else None
        if self.verify == "full" and self.phase in ("copy", "verify"):
            vb = self.total_bytes - (self.done_bytes if self.phase == "verify" else 0)
            eta = (eta or 0) + vb / (self.verify_mbps * 1e6)
        self.report({
            "phase": self.phase, "folder": self.folder,
            "files_done": self.done_files, "files_total": len(self.items),
            "bytes_done": self.done_bytes, "bytes_total": self.total_bytes,
            "percent": round(100 * self.done_bytes / self.total_bytes, 1) if self.total_bytes else 100.0,
            "current_file": self.current,
            "speed_now": round(self.meter.now_bps()), "speed_avg": round(avg),
            "eta": round(eta) if eta is not None else None, **extra})

    # -- helpers -----------------------------------------------------------
    def _pause(self, error: str) -> str:
        self._log(f"PAUSED: {error}")
        self._emit(paused=True, error=error)
        choice = self.decide(error)
        self._log(f"user chose: {choice}")
        self._emit(paused=False, error="")
        return choice

    def _ensure_share_or_pause(self) -> bool:
        """True when the share is usable, False if the user cancelled."""
        while True:
            try:
                self.ensure_share()
                return True
            except Exception as e:  # ShareError or OSError
                if self._pause(str(e)) != "retry":
                    return False

    def _create_folder(self) -> bool:
        label = naming.source_label(self.source.get("label"), self.source.get("model"))
        base = naming.folder_name(dt.datetime.now(), label, self.schema)
        while True:
            try:
                name = naming.unique_folder(
                    base, lambda n: os.path.exists(os.path.join(self.share_root, n)))
                os.makedirs(os.path.join(self.share_root, name))
                self.folder = name
                return True
            except OSError as e:
                if self._pause(f"cannot create the destination folder: {e.strerror or e}") != "retry":
                    return False
                if not self._ensure_share_or_pause():
                    return False

    # -- main --------------------------------------------------------------
    def run(self) -> JobResult:
        t0 = time.monotonic()
        src = self.source
        self._log(f"Ingest Station {__version__}: import of {src.get('name', '?')} "
                  f"(label '{src.get('label', '')}', model '{src.get('model', '')}', "
                  f"serial '{src.get('serial', '')}')")
        self._log(f"{len(self.items)} files, {self.total_bytes} bytes, verification: {self.verify}")
        if not self._ensure_share_or_pause() or not self._create_folder():
            return JobResult(status="cancelled", error="cancelled before copying")
        dest = os.path.join(self.share_root, self.folder)
        self.import_id = self.index.start_import(
            dest_folder=self.folder, verify_mode=self.verify, label=src.get("label", ""),
            model=src.get("model", ""), serial=src.get("serial", ""), kind=src.get("kind", ""),
            files_total=len(self.items), bytes_total=self.total_bytes)
        self._log(f"destination: {self.folder}")
        self._emit()

        status, error = "done", ""
        records: dict[str, str | None] = {}
        try:
            self._copy_all(self.items, dest, records)
            copy_seconds = time.monotonic() - t0
            copy_mbps = self.meter.avg_bps() / 1e6
            verify_mbps = 0.0
            if self.verify == "full":
                verify_mbps = self._verify_all(dest, records)
            self.phase = "finishing"
            self._emit()
        except Cancelled:
            status, error = "cancelled", "cancelled by the user"
            copy_seconds, copy_mbps, verify_mbps = time.monotonic() - t0, 0.0, 0.0
        except SourceError as e:
            status, error = "failed", f"source: {e}"
            copy_seconds, copy_mbps, verify_mbps = time.monotonic() - t0, 0.0, 0.0
        except DestError as e:  # only raised when the user gave up after a pause
            status, error = "failed", str(e)
            copy_seconds, copy_mbps, verify_mbps = time.monotonic() - t0, 0.0, 0.0

        self._log(f"result: {status}{' - ' + error if error else ''}; "
                  f"{self.done_files}/{len(self.items)} files, {self.done_bytes} bytes "
                  f"in {copy_seconds:.0f} s ({copy_mbps:.1f} MB/s)")
        self._write_manifest_and_log(dest)
        self.index.finish_import(self.import_id, status, error or None)
        self._backup_index()
        return JobResult(status=status, folder=self.folder, error=error,
                         files=self.done_files, bytes=self.done_bytes,
                         seconds=time.monotonic() - t0, copy_mbps=copy_mbps,
                         verify_mbps=verify_mbps)

    def _copy_all(self, items: list[Item], dest: str, records: dict) -> None:
        self.phase = "copy"
        want_hash = self.verify != "none"
        i = 0
        while i < len(items):
            if self.cancel.is_set():
                raise Cancelled()
            item = items[i]
            self.current = item.rel
            try:
                res = copier.copy_file(item, dest, want_hash=want_hash, progress=self._progress,
                                       cancel=self.cancel)
            except DestError as e:
                # the partial file is removed; retry rewrites it from scratch
                self.done_bytes = sum(it.size for it in items[:i])
                if self._pause(str(e)) != "retry" or not self._ensure_share_or_pause():
                    raise Cancelled() from e
                continue
            self.index.add_file(self.import_id, res.fp, item.rel, item.size, item.mtime,
                                res.xxh128)
            records[item.rel] = res.xxh128
            self.done_files += 1
            i += 1
        self.current = ""

    def _verify_all(self, dest: str, records: dict) -> float:
        """Read every copy back from the share and compare its xxh128."""
        while True:
            self.phase = "verify"
            self.done_bytes = 0
            vmeter = Meter()
            self.meter = vmeter
            copier.drop_caches()
            bad: list[Item] = []
            by_rel = {it.rel: it for it in self.items}
            try:
                for rel, expected in records.items():
                    if self.cancel.is_set():
                        raise Cancelled()
                    self.current = rel
                    got = copier.hash_file(os.path.join(dest, *rel.split("/")),
                                           progress=self._progress, cancel=self.cancel, rel=rel)
                    if got != expected:
                        bad.append(by_rel[rel])
                        self._log(f"VERIFY MISMATCH: {rel}")
            except DestError as e:
                if self._pause(str(e)) != "retry" or not self._ensure_share_or_pause():
                    raise Cancelled() from e
                continue
            self.current = ""
            ok = [r for r in records if r not in {b.rel for b in bad}]
            self.index.mark_verified(self.import_id, ok)
            if not bad:
                self._log(f"verification OK: {len(records)} files")
                return vmeter.avg_bps() / 1e6
            names = ", ".join(b.rel for b in bad[:3]) + ("..." if len(bad) > 3 else "")
            if self._pause(f"{len(bad)} files differ after the copy ({names})") != "retry":
                raise DestError(f"verification failed for {len(bad)} files")
            if not self._ensure_share_or_pause():
                raise Cancelled()
            # copy the mismatching files again, then verify everything again
            self.meter = Meter()
            self.phase = "copy"
            for item in bad:
                res = copier.copy_file(item, dest, want_hash=True, progress=self._progress,
                                       cancel=self.cancel)
                records[item.rel] = res.xxh128

    def _write_manifest_and_log(self, dest: str) -> None:
        try:
            files = self.index.files_of(self.import_id)
            if self.verify != "none":
                with open(os.path.join(dest, f"{self.folder}.xxh128"), "w",
                          encoding="utf-8", newline="\n") as f:
                    for r in files:
                        if r["xxh128"]:
                            f.write(f"{r['xxh128']}  {r['rel_path']}\n")
            with open(os.path.join(dest, f"{self.folder}.log"), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write("\n".join(self.log) + "\n")
                for r in files:
                    f.write(f"  {r['status']:8} {r['size']:>14} {r['rel_path']}\n")
        except OSError as e:
            self._log(f"warning: cannot write the manifest/log on the share: {e}")

    def _backup_index(self) -> None:
        try:
            d = os.path.join(self.share_root, ".ingest-station")
            os.makedirs(d, exist_ok=True)
            self.index.backup_to(os.path.join(d, "index.db"))
        except Exception as e:  # noqa: BLE001 - a backup failure must not fail the import
            self._log(f"warning: index backup failed: {e}")
