"""Network destination: NFS (default) or SMB/CIFS, on any server (NAS
brands, Windows, Linux...).

With version "auto" the newest protocol version the server accepts is
negotiated: NFS 4.2, 4.1, 4.0, then 3; SMB 3.1.1, 3.0, then 2.1. A version
can be forced in the settings for servers that misbehave.

Mounts are "soft": when the NAS disappears during a copy, I/O fails after a
few seconds instead of hanging forever, so the import can pause with an
error and wait for Retry. Health checks run in a thread with a timeout for
the same reason.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from ingest.common import blockdev, config, sysfs

MOUNT = "/run/ingest/share"
TEST_MOUNT = "/run/ingest/share-test"
MARK_DIR = ".ingest-station"

NFS_VERSIONS = ("4.2", "4.1", "4.0", "3")
SMB_VERSIONS = ("3.1.1", "3.0", "2.1")
NFS_COMMON = "proto=tcp,soft,timeo=50,retrans=2,noatime,rsize=1048576,wsize=1048576"
SMB_COMMON = "soft,echo_interval=10,noperm,uid=0,gid=0,file_mode=0664,dir_mode=0775"

# negotiated protocol per mount point, for display ("NFS 4.1")
negotiated: dict[str, str] = {}


class ShareError(Exception):
    pass


def configured(cfg: dict) -> bool:
    return bool(cfg.get("server") and cfg.get("path"))


def candidates(cfg: dict) -> list[tuple[str, str, str, str]]:
    """Mount attempts in order: (fstype, device, options, label)."""
    server = cfg["server"].strip()
    path = cfg["path"].strip()
    forced = str(cfg.get("version") or "auto")
    if cfg.get("type") == "smb":
        share = path.strip("/\\").replace("\\", "/")
        auth = (f"credentials={config.SMB_CREDENTIALS}"
                if cfg.get("username") and os.path.exists(config.SMB_CREDENTIALS) else "guest")
        versions = SMB_VERSIONS if forced == "auto" else (forced,)
        return [("cifs", f"//{server}/{share}", f"vers={v},{SMB_COMMON},{auth}", f"SMB {v}")
                for v in versions]
    if not path.startswith("/"):
        path = "/" + path
    versions = NFS_VERSIONS if forced == "auto" else (forced,)
    out = []
    for v in versions:
        if v.startswith("4"):
            out.append(("nfs4", f"{server}:{path}", f"vers={v},{NFS_COMMON}", f"NFS {v}"))
        else:
            # v3 without the lock manager: this station is the only writer
            out.append(("nfs", f"{server}:{path}", f"vers={v},nolock,{NFS_COMMON}", f"NFS {v}"))
    return out


def is_mounted(mnt: str = MOUNT) -> bool:
    return any(m == mnt for _d, m, _t, _o in blockdev.mounts())


def mount(cfg: dict, mnt: str = MOUNT, timeout: float = 30) -> None:
    if not configured(cfg):
        raise ShareError("no destination configured")
    if is_mounted(mnt):
        return
    os.makedirs(mnt, exist_ok=True)
    errors = []
    tries = candidates(cfg)
    for n, (fstype, dev, opts, label) in enumerate(tries):
        try:
            res = subprocess.run(["mount", "-t", fstype, "-o", opts, dev, mnt],
                                 capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ShareError(f"{cfg['server']} does not answer") from e
        if res.returncode == 0:
            if fstype.startswith("nfs") and n < len(tries) - 1 and not read_back_ok(cfg, mnt):
                # the server accepts this version but its reads fail (seen
                # with NFS 4.2 READ_PLUS): an import would copy fine and then
                # fail at the verification; try the next version instead
                subprocess.run(["umount", "-f", "-l", mnt], capture_output=True, timeout=30)
                errors.append(f"{label}: the server accepts it but reading fails")
                continue
            negotiated[mnt] = label
            return
        lines = sysfs.decode(res.stderr or res.stdout).strip().splitlines()
        msg = lines[-1] if lines else "mount failed"
        errors.append(msg)
        low = msg.lower()
        if any(w in low for w in ("timed out", "no route", "unreachable", "connection refused",
                                  "name or service", "could not resolve")):
            break   # the server is not there: other versions will not help
    # report the most useful error: not "version not supported" if avoidable
    useful = [e for e in errors if "not supported" not in e.lower()
              and "protocol" not in e.lower()] or errors
    raise ShareError(_friendly(useful[0], cfg))


def read_back_ok(cfg: dict, mnt: str) -> bool:
    """Write a small file on the share and read it back from the server
    (O_DIRECT: not from the page cache). False only when the read itself
    fails or returns other bytes; when the file cannot be written, or the
    share hangs, it is not a protocol version problem and check() reports it."""
    data = f"ingest-station read-back {time.time()}".encode()
    p = os.path.join(dest_root(cfg, mnt), MARK_DIR, f".readback-{os.getpid()}")

    def run() -> bool:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            return True
        try:
            try:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
            except OSError:
                fd = os.open(p, os.O_RDONLY)    # no direct I/O on this file system
            try:
                return os.read(fd, 4096) == data
            finally:
                os.close(fd)
        except OSError:
            return False
        finally:
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        return _with_timeout(run, 15)
    except ShareError:
        return True


def _friendly(msg: str, cfg: dict) -> str:
    low = msg.lower()
    if "permission denied" in low or "access denied" in low:
        return f"access denied by {cfg['server']} (check the export or the credentials)"
    if "no such file" in low or "not found" in low:
        return f"'{cfg['path']}' not found on {cfg['server']}"
    if "timed out" in low or "no route" in low or "unreachable" in low:
        return f"{cfg['server']} is not reachable"
    return msg


def umount(mnt: str = MOUNT) -> None:
    if is_mounted(mnt):
        subprocess.run(["umount", "-f", "-l", mnt], capture_output=True, timeout=30)
    negotiated.pop(mnt, None)


def dest_root(cfg: dict, mnt: str = MOUNT) -> str:
    sub = (cfg.get("subdir") or "").strip("/\\")
    return os.path.join(mnt, *[p for p in sub.replace("\\", "/").split("/") if p])


def _with_timeout(fn, timeout: float):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ShareError("the share does not answer")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def check(cfg: dict, mnt: str = MOUNT, timeout: float = 15, write: bool = True) -> dict:
    """Mount if needed and prove the destination is writable (`write=False`:
    read-only check, used periodically so the NAS disks can spin down).
    Returns {"state": "online"|"offline"|"unconfigured", "reason", "free", "total"}."""
    if not configured(cfg):
        return {"state": "unconfigured", "reason": "No destination configured"}
    try:
        mount(cfg, mnt)
        root = dest_root(cfg, mnt)

        def probe():
            if not write:
                st = os.statvfs(root)
                os.listdir(root)
                return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
            os.makedirs(os.path.join(root, MARK_DIR), exist_ok=True)
            p = os.path.join(root, MARK_DIR, f".probe-{os.getpid()}")
            with open(p, "wb") as f:
                f.write(str(time.time()).encode())
                f.flush()
                os.fsync(f.fileno())
            os.remove(p)
            st = os.statvfs(root)
            return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize

        free, total = _with_timeout(probe, timeout)
        return {"state": "online", "reason": "", "free": free, "total": total,
                "protocol": negotiated.get(mnt, "")}
    except ShareError as e:
        return {"state": "offline", "reason": str(e)}
    except OSError as e:
        if e.errno in (5, 116, 112, 113, 110):  # EIO, ESTALE, EHOSTDOWN, EHOSTUNREACH, ETIMEDOUT
            umount(mnt)
        return {"state": "offline", "reason": f"cannot write to the share: {e.strerror or e}"}


def test(cfg: dict, password: str | None = None) -> dict:
    """Try a configuration without saving it (settings: Test connection)."""
    old_cred = None
    try:
        if cfg.get("type") == "smb" and password is not None:
            if os.path.exists(config.SMB_CREDENTIALS):
                with open(config.SMB_CREDENTIALS, encoding="utf-8") as f:
                    old_cred = f.read()
            config.write_smb_credentials(cfg.get("username", ""), password,
                                         cfg.get("domain", ""))
        umount(TEST_MOUNT)
        return check(cfg, TEST_MOUNT)
    finally:
        umount(TEST_MOUNT)
        if old_cred is not None:
            fd = os.open(config.SMB_CREDENTIALS, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(old_cred)


def list_dirs(cfg: dict, rel: str = "", mnt: str = MOUNT) -> list[str]:
    """Sub-folders of `rel` inside the share (folder browser)."""
    mount(cfg, mnt)
    base = os.path.join(mnt, *[p for p in rel.replace("\\", "/").split("/") if p and p != ".."])

    def ls():
        with os.scandir(base) as it:
            return sorted(e.name for e in it if e.is_dir() and not e.name.startswith("."))
    return _with_timeout(ls, 15)


def make_dir(cfg: dict, rel: str, mnt: str = MOUNT) -> None:
    mount(cfg, mnt)
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != ".."]
    _with_timeout(lambda: os.makedirs(os.path.join(mnt, *parts), exist_ok=True), 15)
