"""Client of the ingestd Unix socket (used by the GUI, the web server and
ingestctl)."""

from __future__ import annotations

import itertools
import json
import socket

SOCKET = "/run/ingestd.sock"


class DaemonError(Exception):
    pass


class Client:
    def __init__(self, path: str = SOCKET, timeout: float = 120):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise DaemonError(f"ingestd is not running ({e.strerror or e})") from e
        self.file = self.sock.makefile("rb")
        self._ids = itertools.count(1)

    def close(self) -> None:
        self.file.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, cmd: str, **args):
        rid = next(self._ids)
        self.sock.sendall((json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n").encode())
        while True:
            line = self.file.readline()
            if not line:
                raise DaemonError("connection closed by ingestd")
            msg = json.loads(line)
            if msg.get("id") == rid:
                if not msg.get("ok"):
                    raise DaemonError(msg.get("error", "error"))
                return msg.get("result")

    def events(self):
        """Subscribe and yield state snapshots forever."""
        self.sock.sendall(b'{"cmd": "subscribe"}\n')
        self.sock.settimeout(None)
        for line in self.file:
            msg = json.loads(line)
            if msg.get("event") == "state":
                yield msg["data"]
