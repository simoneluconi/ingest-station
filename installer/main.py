"""Install the system from the USB stick to the internal eMMC.

Flow: find the eMMC robustly -> verify the USB copy -> cancellable countdown
-> wipefs -> write test (worn-out eMMC detection) -> GPT (ESP + data) ->
format -> copy slot A + bootloader -> verify -> reboot.

Nothing is written unless exactly one suitable eMMC is found. The log is
always saved on the USB stick. Device model names are never read (the Wyse
3040 eMMC reports a non-UTF-8 name).
"""

from __future__ import annotations

import fcntl
import os
import select
import shutil
import sys
import termios
import time
import traceback
import tty

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest import __version__  # noqa: E402
from ingest.common import blockdev, efiboot, grubenv, slots, sysfs  # noqa: E402

ESP_MIB = 1536
ESP_LABEL = "INGEST_ESP"
DATA_LABEL = "ingest-data"
ESP_MNT = "/mnt/ingest-esp"
DATA_MNT = "/mnt/ingest-data"
COUNTDOWN = 10
BLKFLSBUF = 0x1261
TEST_OFFSET = 1 << 20
TEST_SIZE = 1 << 20


class InstallError(Exception):
    """Expected failure with a message for the user."""


class Log:
    def __init__(self):
        self.lines: list[str] = []
        # test builds (build.py --test-vm): mirror the log on the serial port
        self.serial = "ingest_test_serial" in sysfs.kernel_cmdline()

    def __call__(self, msg: str = "", screen: bool = True) -> None:
        stamp = time.strftime("%H:%M:%S")
        for line in (msg.splitlines() or [""]):
            self.lines.append(f"{stamp} {line}")
            if screen:
                print(f"  {line}", flush=True)
            self._kmsg(line)
            if self.serial:
                self._serial(line)

    @staticmethod
    def _serial(line: str) -> None:
        try:
            with open("/dev/ttyS0", "w", encoding="utf-8") as t:
                t.write(f"ingest-install: {line}\r\n")
        except OSError:
            pass

    @staticmethod
    def _kmsg(line: str) -> None:
        # info level: kept in dmesg, not printed on the console with loglevel=3
        try:
            with open("/dev/kmsg", "w", encoding="utf-8") as k:
                k.write(f"<6>ingest-install: {line}\n")
        except OSError:
            pass

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# --------------------------------------------------------------------------
# helpers

def run(log: Log, cmd: list[str], check: bool = True) -> str:
    log(f"$ {' '.join(cmd)}", screen=False)
    try:
        out = sysfs.run(cmd, check=check, timeout=600)
    except Exception as e:
        out = getattr(e, "output", "") or str(e)
        log(out.strip(), screen=False)
        raise InstallError(f"command failed: {' '.join(cmd)}\n{out.strip()}") from e
    if out.strip():
        log(out.strip(), screen=False)
    return out


def wait_key(timeout: float | None) -> bool:
    """Wait for a key press on stdin; True if a key was pressed."""
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        if timeout is None:
            return True
        time.sleep(timeout)
        return False
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        ready, _, _ = select.select([fd], [], [], timeout)
        if ready:
            os.read(fd, 64)
            return True
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def find_usb_media() -> tuple[str, str]:
    """(mount point, disk name) of the installation stick."""
    for dev, mnt, fstype, _ in blockdev.mounts():
        if fstype == "vfat" and os.path.isfile(os.path.join(mnt, "slots", "A", slots.SLOT_MANIFEST)) \
                and not os.path.isfile(os.path.join(mnt, slots.GRUBENV)):
            return mnt, blockdev.parent_disk(os.path.basename(dev))
    raise InstallError("installation media not found")


def wait_for_emmc(log: Log, seconds: float = 10) -> None:
    for mod in ("sdhci", "sdhci-acpi", "mmc_block"):
        sysfs.run(["modprobe", mod], check=False)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(n.startswith("mmcblk") for n in sysfs.listdir("/sys/block")):
            return
        time.sleep(0.5)
    log("warning: no mmcblk device appeared after loading the eMMC drivers")


def choose_target(log: Log, usb_disk: str) -> blockdev.Disk:
    disks = blockdev.list_disks(read_model=False)
    log("Disks found:", screen=False)
    for d in disks:
        log(f"  {d.describe()}", screen=False)
    cands, rejected = blockdev.install_candidates(disks, {usb_disk})
    for d, why in rejected:
        log(f"  skipped {d.name}: {why}", screen=False)
    # test builds only (build.py --test-vm): QEMU's emulated eMMC is reported
    # as removable; accept the named MMC device anyway
    forced = sysfs.kernel_cmdline().get("ingest_test_target")
    if forced:
        for d, why in rejected:
            if d.name == forced and why == "removable" and d.name != usb_disk:
                log(f"TEST BUILD: accepting {d.name} despite: {why}")
                return d
    if len(cands) == 1:
        return cands[0]
    listing = "\n".join(f"  {d.describe()}" for d in disks) or "  (no disks at all)"
    reason = "No suitable internal eMMC was found." if not cands else \
        "More than one possible target disk was found."
    raise InstallError(f"{reason} Nothing was written.\nDisks seen:\n{listing}")


def partition_dev(disk: str, n: int) -> str:
    return f"/dev/{blockdev.partition_name(disk, n)}"


def wait_for_partitions(log: Log, disk: str, count: int, stable: float = 2.0) -> None:
    """Wait until the partition nodes exist and stay present: the kernel
    removes and re-adds them while it re-reads the partition table."""
    devs = [partition_dev(disk, n) for n in range(1, count + 1)]
    deadline = time.monotonic() + 30
    since = None
    while time.monotonic() < deadline:
        if all(os.path.exists(d) for d in devs):
            since = since or time.monotonic()
            if time.monotonic() - since >= stable:
                return
        else:
            since = None
            sysfs.run(["mdev", "-s"], check=False)
        time.sleep(0.2)
    raise InstallError(f"partition devices did not appear: {', '.join(devs)}")


def unmount_disk(log: Log, disk: str) -> None:
    """Unmount every partition of `disk`; stop before erasing anything if
    one of them stays in use."""
    for dev, mnt, _, _ in reversed(blockdev.mounts()):
        if os.path.basename(dev).startswith(disk):
            run(log, ["umount", mnt], check=False)
    busy = [f"{dev} on {mnt}" for dev, mnt, _, _ in blockdev.mounts()
            if os.path.basename(dev).startswith(disk)]
    if busy:
        raise InstallError("The eMMC is in use and cannot be erased: " + ", ".join(busy)
                           + ". Nothing was erased; restart and try again.")


def write_test(log: Log, disk: str) -> None:
    """Quirk: a worn eMMC can turn read-only, sometimes silently dropping
    writes. Write a random block, flush all caches and read it back."""
    path = f"/dev/{disk}"
    pattern = os.urandom(TEST_SIZE)
    try:
        fd = os.open(path, os.O_RDWR | os.O_SYNC)
        try:
            os.pwrite(fd, pattern, TEST_OFFSET)
            os.fsync(fd)
            fcntl.ioctl(fd, BLKFLSBUF)
        finally:
            os.close(fd)
        slots.drop_caches()
        fd = os.open(path, os.O_RDONLY)
        try:
            back = os.pread(fd, TEST_SIZE, TEST_OFFSET)
        finally:
            os.close(fd)
    except OSError as e:
        raise InstallError(f"The eMMC rejected a test write ({e.strerror}). It is probably "
                           "worn out and read-only. Installation stopped.") from e
    if back != pattern:
        raise InstallError("The eMMC does not keep written data: it is probably worn out "
                           "and read-only. Installation stopped.")


def copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
        fo.flush()
        os.fsync(fo.fileno())


def copy_tree(log: Log, src: str, dst: str) -> None:
    files = []
    for root, _dirs, names in os.walk(src):
        for n in names:
            files.append(os.path.relpath(os.path.join(root, n), src))
    total = sum(os.path.getsize(os.path.join(src, f)) for f in files)
    done = 0
    for rel in sorted(files):
        copy_file(os.path.join(src, rel), os.path.join(dst, rel))
        done += os.path.getsize(os.path.join(src, rel))
        print(f"\r  Copying system... {done * 100 // max(total, 1):3d}%", end="", flush=True)
    print()
    log(f"copied {len(files)} files, {total / 1e6:.0f} MB", screen=False)


def blkid_uuid(dev: str) -> str:
    return sysfs.run(["blkid", "-s", "UUID", "-o", "value", dev]).strip()


# --------------------------------------------------------------------------
# main flow

def install(log: Log, usb_mnt: str, target: blockdev.Disk) -> None:
    disk = target.name
    src_slot = os.path.join(usb_mnt, "slots", "A")
    esp_dev, data_dev = partition_dev(disk, 1), partition_dev(disk, 2)

    log("Erasing old signatures (wipefs)...")
    unmount_disk(log, disk)
    for part in sorted(target.partitions, reverse=True):
        run(log, ["wipefs", "-a", f"/dev/{part}"], check=False)
    run(log, ["wipefs", "-a", f"/dev/{disk}"])

    log("Checking that the eMMC is writable...")
    write_test(log, disk)

    log("Creating partitions...")
    run(log, ["sgdisk", "--zap-all", f"/dev/{disk}"])
    run(log, ["sgdisk",
              "-n", f"1:2048:+{ESP_MIB}M", "-t", "1:ef00", "-c", f"1:{ESP_LABEL}",
              "-n", "2:0:0", "-t", "2:8300", "-c", f"2:{DATA_LABEL}",
              f"/dev/{disk}"])
    wait_for_partitions(log, disk, 2)

    log("Formatting...")
    run(log, ["mkfs.vfat", "-F", "32", "-n", ESP_LABEL, esp_dev])
    wait_for_partitions(log, disk, 2, stable=0.5)
    run(log, ["mkfs.ext4", "-F", "-q", "-L", DATA_LABEL, "-m", "0", "-E", "nodiscard", data_dev])
    data_uuid = blkid_uuid(data_dev)
    log(f"ESP UUID {blkid_uuid(esp_dev)}, data UUID {data_uuid}", screen=False)

    os.makedirs(ESP_MNT, exist_ok=True)
    run(log, ["mount", "-t", "vfat", esp_dev, ESP_MNT])
    try:
        copy_tree(log, src_slot, os.path.join(ESP_MNT, "slots", "A"))
        slots.install_bootloader(src_slot, ESP_MNT)
        # empty marker: lets Alpine's boot media scan (nlplug-findfs) finish
        # at once instead of waiting for its timeout (~4 s per boot)
        open(os.path.join(ESP_MNT, "slots", ".boot_repository"), "wb").close()
        grubenv.create(os.path.join(ESP_MNT, slots.GRUBENV),
                       {"active_slot": "A", "data_uuid": data_uuid})
        os.sync()
    finally:
        run(log, ["umount", ESP_MNT], check=False)

    log("Verifying the installed system...")
    slots.drop_caches()
    run(log, ["mount", "-t", "vfat", "-o", "ro", esp_dev, ESP_MNT])
    try:
        problems = slots.verify_slot(os.path.join(ESP_MNT, "slots", "A"))
        env = grubenv.read(os.path.join(ESP_MNT, slots.GRUBENV))
        if env.get("data_uuid") != data_uuid:
            problems.append("grubenv verification failed")
        for rel in ("EFI/BOOT/BOOTX64.EFI", "boot/grub/grub.cfg"):
            if not os.path.isfile(os.path.join(ESP_MNT, *rel.split("/"))):
                problems.append(f"missing {rel}")
    finally:
        run(log, ["umount", ESP_MNT], check=False)
    if problems:
        raise InstallError("Verification of the installed files failed (worn eMMC?):\n  "
                           + "\n  ".join(problems[:10]))

    register_boot_entry(log, disk)

    os.makedirs(DATA_MNT, exist_ok=True)
    run(log, ["mount", "-t", "ext4", data_dev, DATA_MNT])
    try:
        for d in ("config", "log", "index", "keys"):
            os.makedirs(os.path.join(DATA_MNT, d), exist_ok=True)
        with open(os.path.join(DATA_MNT, "log", "install.log"), "w", encoding="utf-8") as f:
            f.write(log.text())
    finally:
        run(log, ["umount", DATA_MNT], check=False)


def register_boot_entry(log: Log, disk: str) -> None:
    """Add an "Ingest Station" UEFI boot entry, first in BootOrder. Not
    fatal: the firmware can still boot the fallback path."""
    log("Registering the UEFI boot entry...")
    if not os.path.isdir("/sys/firmware/efi"):
        log("warning: not booted in UEFI mode, boot entry not registered")
        return
    try:
        current = run(log, ["efibootmgr"])
        for num in efiboot.entries_with_label(current):
            run(log, ["efibootmgr", "--bootnum", num, "--delete-bootnum"])
        run(log, efiboot.create_command(disk, 1))
    except InstallError as e:
        log(f"warning: could not register the UEFI boot entry ({e}); "
            "the firmware will use \\EFI\\BOOT\\BOOTX64.EFI")


def save_log(log: Log, usb_mnt: str | None) -> None:
    if not usb_mnt:
        return
    name = time.strftime("ingest-install-%Y%m%d-%H%M%S.log")
    try:
        sysfs.run(["mount", "-o", "remount,rw", usb_mnt], check=False)
        with open(os.path.join(usb_mnt, name), "w", encoding="utf-8") as f:
            f.write(log.text())
            f.flush()
            os.fsync(f.fileno())
        print(f"  Log saved on the USB stick: {name}")
    except OSError as e:
        print(f"  Could not save the log on the USB stick: {e}")
    finally:
        os.sync()
        sysfs.run(["mount", "-o", "remount,ro", usb_mnt], check=False)


def main() -> int:
    log = Log()
    usb_mnt = None
    ok = False
    print("\n\n  ===  Ingest Station installer  " + __version__ + "  ===\n", flush=True)
    try:
        usb_mnt, usb_disk = find_usb_media()
        log(f"installation media: {usb_mnt} (disk {usb_disk})", screen=False)
        log("Looking for the internal eMMC...")
        wait_for_emmc(log)
        target = choose_target(log, usb_disk)
        if target.read_only:
            raise InstallError(f"The eMMC ({target.name}) is read-only (worn out?). "
                               "Nothing was written.")

        log("Checking the installation files on the USB stick...")
        problems = slots.verify_slot(os.path.join(usb_mnt, "slots", "A"))
        if problems:
            raise InstallError("The USB stick is damaged, copy dist/usb again:\n  "
                               + "\n  ".join(problems[:10]))

        print()
        print(f"  Installing to eMMC {target.size_gb:.1f} GB ({target.name})")
        print("  ALL DATA ON THE eMMC WILL BE ERASED.")
        print()
        countdown = int(sysfs.kernel_cmdline().get("ingest_test_countdown") or COUNTDOWN)
        for s in range(countdown, 0, -1):
            print(f"\r  Starting in {s:2d} s - press any key to CANCEL ", end="", flush=True)
            if wait_key(1):
                print()
                log("Installation cancelled by the user. Nothing was written.")
                return 1
        print("\n")
        log(f"installing to {target.name} ({target.size} bytes)", screen=False)
        install(log, usb_mnt, target)
        ok = True
        log("")
        log("Installation complete.")
    except InstallError as e:
        log("")
        log("INSTALLATION FAILED")
        log(str(e))
    except Exception:
        log("")
        log("INSTALLATION FAILED (unexpected error)")
        log(traceback.format_exc())
    finally:
        save_log(log, usb_mnt)

    if ok:
        print("\n  Remove the USB stick, then press any key to restart.")
        wait_key(None)
        sysfs.run(["reboot"], check=False)
        return 0
    print("\n  Press any key for a shell.")
    wait_key(None)
    return 1


if __name__ == "__main__":
    sys.exit(main())
