"""Station settings, stored on the data partition (survive updates).

Plain JSON, written atomically. SMB passwords are kept in a separate
credentials file with mode 600, never in the settings file.
"""

from __future__ import annotations

import copy
import json
import os

DATA = os.environ.get("INGEST_DATA", "/data")
CONFIG_DIR = os.path.join(DATA, "config")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")
SMB_CREDENTIALS = os.path.join(CONFIG_DIR, "smb-credentials")
INDEX_DB = os.path.join(DATA, "index", "ingest.db")
LOG_DIR = os.path.join(DATA, "log")

VERIFY_MODES = ("none", "hash", "full")

DEFAULTS: dict = {
    "auto_import": False,
    "auto_import_delay": 10,
    "verify_default": "hash",
    "folder_schema": "{date}_{time}_{label}",
    "share": {
        "type": "nfs",          # nfs | smb
        "server": "",
        "path": "",             # NFS export (/mnt/user/Ingest) or SMB share name
        "subdir": "",           # destination folder inside the share
        "username": "",         # SMB only; the password is in SMB_CREDENTIALS
        "domain": "",           # SMB only, optional (Windows domains)
        "version": "auto",      # auto, or NFS 4.2/4.1/4.0/3, SMB 3.1.1/3.0/2.1
    },
    "beep": {"backend": "none"},   # none | pcspkr
    "web": {"port": 80, "commands_enabled": False},
    "stats": {"copy_mbps": 60.0, "verify_mbps": 90.0},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str = SETTINGS) -> dict:
    try:
        with open(path, "rb") as f:
            stored = json.loads(f.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        stored = {}
    return _merge(DEFAULTS, stored if isinstance(stored, dict) else {})


def save(settings: dict, path: str = SETTINGS) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update(changes: dict, path: str = SETTINGS) -> dict:
    """Merge `changes` into the stored settings (validated) and save."""
    new = _merge(load(path), changes)
    if new["verify_default"] not in VERIFY_MODES:
        raise ValueError(f"verify_default must be one of {VERIFY_MODES}")
    if new["share"]["type"] not in ("nfs", "smb"):
        raise ValueError("share type must be nfs or smb")
    new["auto_import_delay"] = max(3, min(60, int(new["auto_import_delay"])))
    save(new, path)
    return new


def write_smb_credentials(username: str, password: str, domain: str = "",
                          path: str = SMB_CREDENTIALS) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"username={username}\npassword={password}\n")
        if domain:
            f.write(f"domain={domain}\n")
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)
