#!/usr/bin/env python3
"""QEMU harness for smoke-testing dist/usb-vm/ (build.py --test-vm) on the
Windows build host. Not a unit test: it drives a real VM.

The VM approximates the Wyse 3040: UEFI (OVMF), 2 GB RAM, an emulated eMMC
behind an SDHCI controller, the USB stick as a USB mass storage device, an
ICH9 chipset with the iTCO watchdog.

    python tests/vm/qemu_vm.py start [--usb DIR] [--fresh]   run QEMU (blocks)
    python tests/vm/qemu_vm.py wait REGEX [--timeout S]      wait for serial output
    python tests/vm/qemu_vm.py sh "COMMAND" [--timeout S]    run a shell command (serial root login)
    python tests/vm/qemu_vm.py key KEY [KEY...]              send keys to the screen (tty1)
    python tests/vm/qemu_vm.py shot FILE.png                 screenshot
    python tests/vm/qemu_vm.py mon "MONITOR COMMAND"         raw QEMU monitor command
    python tests/vm/qemu_vm.py stop                          quit QEMU cleanly
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(ROOT, "work", "vm")
QEMU_DIR = r"C:\Program Files\qemu"
SERIAL_PORT, MONITOR_PORT = 4445, 4444
LOG = os.path.join(WORK, "serial.log")
EMMC = os.path.join(WORK, "emmc.qcow2")
VARS = os.path.join(WORK, "ovmf-vars.fd")
EMMC_SIZE = 8 << 30


def start(args) -> int:
    os.makedirs(WORK, exist_ok=True)
    if args.fresh or not os.path.exists(EMMC):
        # qcow2: a raw 8 GB file would be fully allocated on NTFS
        if os.path.exists(EMMC):
            os.remove(EMMC)
        subprocess.run([os.path.join(QEMU_DIR, "qemu-img.exe"), "create", "-q", "-f", "qcow2",
                        EMMC, str(EMMC_SIZE)], check=True)
        shutil.copyfile(os.path.join(QEMU_DIR, "share", "edk2-i386-vars.fd"), VARS)
    if os.path.exists(LOG):
        os.remove(LOG)
    cmd = [
        os.path.join(QEMU_DIR, "qemu-system-x86_64.exe"),
        "-nodefaults", "-machine", "q35", "-accel", "tcg",
        "-m", "2048", "-smp", "2",
        "-drive", f"if=pflash,format=raw,readonly=on,file={QEMU_DIR}\\share\\edk2-x86_64-code.fd",
        "-drive", f"if=pflash,format=raw,file={VARS}",
        # the emulated eMMC never sends flushes: always stop the VM with
        # `qemu_vm.py stop` (monitor quit flushes), never by killing QEMU
        "-drive", f"if=none,id=emmc,format=qcow2,file={EMMC}",
        "-device", "qemu-xhci,id=xhci",
        "-netdev", "user,id=n0", "-device", "e1000,netdev=n0",
        "-global", "ICH9-LPC.noreboot=off",
        "-chardev", f"socket,id=ser0,host=127.0.0.1,port={SERIAL_PORT},server=on,wait=off,logfile={LOG}",
        "-serial", "chardev:ser0",
        "-monitor", f"tcp:127.0.0.1:{MONITOR_PORT},server=on,wait=off",
        "-display", "none", "-vga", "std",
    ]
    if args.sata:
        # OVMF has no SD/MMC driver: to boot the installed system, attach the
        # same image as a SATA disk (the system finds its partitions by UUID)
        cmd += ["-device", "ide-hd,drive=emmc,bus=ide.0,bootindex=1"]
    else:
        cmd += ["-device", "sdhci-pci", "-device", "emmc,drive=emmc"]
    if args.usb:
        cmd += ["-drive", f"if=none,id=stick,format=raw,readonly=on,file.driver=vvfat,"
                          f"file.dir={os.path.abspath(args.usb)},file.rw=off,file.fat-type=16",
                "-device", "usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0"]
    print(" ".join(cmd), flush=True)
    return subprocess.call(cmd)


def serial_text() -> str:
    try:
        with open(LOG, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def wait(pattern: str, timeout: float, since: int = 0) -> str | None:
    rx = re.compile(pattern)
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = serial_text()[since:]
        m = rx.search(text)
        if m:
            return m.group(0)
        time.sleep(1)
    return None


def monitor(command: str) -> str:
    with socket.create_connection(("127.0.0.1", MONITOR_PORT), timeout=10) as s:
        s.settimeout(2)
        buf = b""
        try:
            while b"(qemu)" not in buf:
                buf += s.recv(4096)
        except socket.timeout:
            pass
        s.sendall(command.encode() + b"\n")
        time.sleep(1)
        out = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
    return out.decode(errors="replace")


_ANSI = re.compile(r"\x1b\[[0-9;?=]*[A-Za-z]|\r")


def _read_until(s: socket.socket, pattern: str, timeout: float) -> str:
    """Read from the serial socket until `pattern` matches the cleaned text.
    Answers the cursor position query (ESC[6n) sent by busybox ash."""
    buf, deadline = "", time.time() + timeout
    while time.time() < deadline:
        try:
            data = s.recv(4096)
        except socket.timeout:
            continue
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        if "\x1b[6n" in text:
            s.sendall(b"\x1b[1;1R")
        buf += text
        if re.search(pattern, _ANSI.sub("", buf)):
            break
    return _ANSI.sub("", buf)


def sh(command: str, timeout: float) -> int:
    """Run a command on the serial console, logging in as root if needed."""
    with socket.create_connection(("127.0.0.1", SERIAL_PORT), timeout=10) as s:
        s.settimeout(0.5)
        s.sendall(b"\r")
        out = _read_until(s, r"(login: *|# *)$", 15)
        if re.search(r"login: *$", out):
            s.sendall(b"root\r")
            _read_until(s, r"# *$", 20)
        marker = f"__END{int(time.time())}__"
        s.sendall(f"{command}; echo {marker}=$?\r".encode())
        out = _read_until(s, re.escape(marker) + r"=(\d+)", timeout)
    m = re.search(re.escape(marker) + r"=(\d+)", out)
    # output lies between the echoed command (ending in "=$?") and the result
    start = out.find(marker + "=$?")
    start = out.find("\n", start) + 1 if start >= 0 else 0
    end = m.start() if m else len(out)
    print(out[start:end].rstrip())
    if not m:
        print("(timeout)")
        return 124
    return int(m.group(1))


def shot(path: str) -> None:
    ppm = os.path.join(WORK, "screen.ppm")
    if os.path.exists(ppm):
        os.remove(ppm)
    monitor(f"screendump {ppm}")
    for _ in range(10):
        if os.path.exists(ppm) and os.path.getsize(ppm) > 0:
            break
        time.sleep(0.5)
    data = open(ppm, "rb").read()
    parts = data.split(maxsplit=4)
    w, h = int(parts[1]), int(parts[2])
    pix = parts[4][:w * h * 3]
    raw = b"".join(b"\0" + pix[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print(f"{path}: {w}x{h}")


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--usb")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--sata", action="store_true", help="attach the eMMC image as a SATA disk")
    w = sub.add_parser("wait")
    w.add_argument("pattern")
    w.add_argument("--timeout", type=float, default=300)
    c = sub.add_parser("sh")
    c.add_argument("command")
    c.add_argument("--timeout", type=float, default=120)
    k = sub.add_parser("key")
    k.add_argument("keys", nargs="+")
    sh_ = sub.add_parser("shot")
    sh_.add_argument("file")
    sub.add_parser("stop", help="quit QEMU cleanly")
    m = sub.add_parser("mon")
    m.add_argument("command")
    a = p.parse_args()
    if a.cmd == "start":
        return start(a)
    if a.cmd == "wait":
        found = wait(a.pattern, a.timeout)
        print(found if found else "(timeout)")
        return 0 if found else 1
    if a.cmd == "sh":
        return sh(a.command, a.timeout)
    if a.cmd == "key":
        for key in a.keys:
            monitor(f"sendkey {key}")
        return 0
    if a.cmd == "shot":
        shot(a.file)
        return 0
    if a.cmd == "stop":
        try:
            monitor("quit")
        except OSError:
            pass
        return 0
    if a.cmd == "mon":
        print(monitor(a.command))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
