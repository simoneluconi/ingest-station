#!/usr/bin/env python3
"""Talk to an Ingest Station over SSH with the build key.

    python tools/wyse.py sh HOST "COMMAND"        run a command (root)
    python tools/wyse.py status HOST              slot status
    python tools/wyse.py deploy HOST [--no-reboot] [--tries N] [--slot DIR]
                                                  over-the-air update from dist/usb

HOST may be "ip" or "ip:port". The update sends only the files that differ
from the running slot; the station completes the new system with its
unchanged files, verifies every checksum, installs it in the inactive slot
and boots it on trial (automatic rollback if it does not come up healthy).
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
import time

import logging

import paramiko

# paramiko logs a traceback for every refused connection while the station
# reboots; the deploy loop handles those itself
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.path.join(ROOT, "keys", "private", "ssh_ed25519")
KNOWN_HOSTS = os.path.join(ROOT, "keys", "private", "known_hosts")
DEFAULT_SLOT = os.path.join(ROOT, "dist", "usb", "slots", "A")


class DeployError(Exception):
    pass


def connect(host: str, timeout: float = 15) -> paramiko.SSHClient:
    name, _, port = host.partition(":")
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
    # trust on first use; a changed key (e.g. after a reinstall) is refused:
    # remove the line from keys/private/known_hosts in that case
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(name, port=int(port or 22), username="root", key_filename=KEY,
                   look_for_keys=False, allow_agent=False, timeout=timeout)
    client.save_host_keys(KNOWN_HOSTS)
    return client


def run(client: paramiko.SSHClient, command: str, echo: bool = True) -> tuple[int, str]:
    _, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if echo:
        sys.stdout.write(out)
        sys.stderr.write(err)
    return rc, out + err


def local_manifest(slot_dir: str) -> dict:
    with open(os.path.join(slot_dir, "slot.json"), encoding="utf-8") as f:
        return json.load(f)


def wait_for_ssh(host: str, timeout: float) -> paramiko.SSHClient:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return connect(host, timeout=10)
        except (OSError, paramiko.SSHException):
            time.sleep(5)
    raise DeployError(f"{host} did not come back within {timeout:.0f} s")


def deploy(host: str, slot_dir: str, tries: int = 3, reboot: bool = True,
           wait: float = 600) -> int:
    new = local_manifest(slot_dir)
    client = connect(host)
    rc, out = run(client, "ingest-slot stage-begin", echo=False)
    if rc != 0:
        raise DeployError(out.strip())
    info = json.loads(out)
    running = info["manifest"].get("files", {})
    changed = [rel for rel, digest in new["files"].items() if running.get(rel) != digest]
    size = sum(os.path.getsize(os.path.join(slot_dir, *rel.split("/"))) for rel in changed)
    print(f"{host}: running slot {info['current']} ({info['manifest'].get('version', '?')}), "
          f"installing {new['version']} into slot {info['target']}")
    print(f"sending {len(changed)} of {len(new['files'])} files ({size / 2**20:.1f} MiB)")

    sftp = client.open_sftp()
    made = set()
    try:
        for rel in changed + ["slot.json"]:
            remote = posixpath.join(info["staging"], rel)
            parent = posixpath.dirname(remote)
            if parent not in made:
                run(client, f"mkdir -p '{parent}'", echo=False)
                made.add(parent)
            sftp.put(os.path.join(slot_dir, *rel.split("/")), remote)
            print(f"  {rel}")
    except Exception:
        run(client, "ingest-slot stage-abort", echo=False)
        raise
    finally:
        sftp.close()

    rc, out = run(client, f"ingest-slot stage-commit --tries {tries}"
                          + (" --reboot" if reboot else ""))
    client.close()
    if rc != 0:
        raise DeployError("the station refused the update")
    if not reboot:
        print("staged; reboot the station to start the trial")
        return 0

    print("waiting for the station to reboot...")
    time.sleep(20)
    client = wait_for_ssh(host, wait)
    deadline = time.monotonic() + wait
    while True:
        _, out = run(client, "ingest-slot status", echo=False)
        st = json.loads(out)
        if st["try_failed"] == info["target"]:
            print(f"UPDATE ROLLED BACK: slot {info['target']} did not boot healthy; "
                  f"running slot {st['current']} again")
            return 1
        if st["active"] == info["target"] and not st["try_slot"] and st["current"] == info["target"]:
            print(f"update confirmed: slot {info['target']} running {new['version']}")
            _, log = run(client, "tail -1 /data/log/boot.log", echo=False)
            print(log.strip())
            return 0
        if time.monotonic() > deadline:
            print(f"no confirmation yet, status: {json.dumps(st)}")
            return 1
        time.sleep(5)
        try:
            client.exec_command("true")
        except Exception:
            client = wait_for_ssh(host, wait)


def push(host: str, restart: list[str]) -> int:
    """Development shortcut: copy the application code into the running
    system (it lives in RAM: a reboot restores the installed version) and
    restart the given services. Real updates go through `deploy`."""
    client = connect(host)
    sftp = client.open_sftp()
    pairs = [(os.path.join(ROOT, "src", "ingest"), "/opt/ingest/ingest"),
             (os.path.join(ROOT, "installer"), "/opt/ingest/installer")]
    for sub in ("etc/init.d", "usr/local/bin", "usr/local/sbin"):
        pairs.append((os.path.join(ROOT, "overlay", *sub.split("/")), "/" + sub))
    n = 0
    for local_root, remote_root in pairs:
        for dirpath, dirnames, files in os.walk(local_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, local_root).replace(os.sep, "/")
            rdir = remote_root if rel == "." else f"{remote_root}/{rel}"
            run(client, f"mkdir -p '{rdir}'", echo=False)
            for f in files:
                if f.endswith(".pyc"):
                    continue
                sftp.put(os.path.join(dirpath, f), f"{rdir}/{f}")
                if remote_root.startswith(("/etc/init.d", "/usr/local")):
                    sftp.chmod(f"{rdir}/{f}", 0o755)
                n += 1
    sftp.close()
    print(f"pushed {n} files")
    for svc in restart:
        run(client, f"rc-service {svc} restart 2>&1 | tail -2")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest Station over SSH")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sh")
    s.add_argument("host")
    s.add_argument("command")
    st = sub.add_parser("status")
    st.add_argument("host")
    d = sub.add_parser("deploy")
    d.add_argument("host")
    d.add_argument("--slot", default=DEFAULT_SLOT, help="built slot to send (default dist/usb)")
    d.add_argument("--tries", type=int, default=3)
    d.add_argument("--no-reboot", action="store_true")
    pu = sub.add_parser("push", help="dev: copy the code into the running system")
    pu.add_argument("host")
    pu.add_argument("--restart", nargs="*", default=[], metavar="SERVICE")
    a = p.parse_args()
    try:
        if a.cmd == "sh":
            return run(connect(a.host), a.command)[0]
        if a.cmd == "status":
            return run(connect(a.host), "ingest-slot status")[0]
        if a.cmd == "push":
            return push(a.host, a.restart)
        return deploy(a.host, a.slot, a.tries, not a.no_reboot)
    except (DeployError, paramiko.SSHException, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
