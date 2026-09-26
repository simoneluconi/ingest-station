# Phase 1 - hardware test on the Wyse 3040

Goal: verify the installer, the boot from eMMC, the A/B slots with rollback
and the hardware quirk fixes. There is no GUI yet: everything happens on the
text console.

Console: `tty1` shows the banner and a login prompt, `Alt+F2` opens `tty2`.
Log in as `root` (no password; physical access only).

## 1. Installation

1. Build (`python build.py`), copy the content of `dist/usb/` to a FAT32
   stick, boot it with F12, choose *Install*.
2. Check:
   - [ ] the installer shows `Installing to eMMC ... (mmcblkN)` with the
         right size, and pressing a key during the countdown cancels it
         (then reboot and install for real);
   - [ ] the installation completes; `ingest-install-*.log` is on the stick;
   - [ ] after removing the stick and restarting, the station boots from
         the eMMC without pressing F12.

## 2. First boot from eMMC

- [ ] The tty1 banner shows `Ingest Station 0.1.0   slot A` and the IP.
- [ ] `ping ingest.local` works from a PC on the LAN.
- [ ] `ingest-slot status` shows `"active": "A"`, `"current": "A"`.
- [ ] `cat /data/log/boot.log` contains `boot OK on slot A at N s`.
- [ ] `dmesg | grep ingest` shows `watchdog armed (300s)` and
      `booting slot A from /dev/mmcblk...`. If it says `no iTCO watchdog`,
      please report it (rollback then relies on manual power cycles).
- [ ] `lsmod | grep -E 'sdhci|mmc_block|pcspkr'` lists the modules;
      `lsmod | grep dw_dmac` lists nothing.

## 3. Reboot and power-off (dw_dmac quirk)

- [ ] `reboot` restarts cleanly (repeat 3 times).
- [ ] `poweroff` switches the unit off.
- [ ] Unplugging the power while running and plugging it back boots normally.

## 4. A/B slots

```sh
ingest-slot clone          # copies slot A to slot B and verifies it (~1 min)
ingest-slot try B          # B on trial, 3 attempts
reboot
```

- [ ] After the reboot the banner says `slot B`; after ~30 s
      `ingest-slot status` shows `"active": "B"` and no `try_slot`.
- [ ] `ingest-slot rollback` then `reboot` goes back to slot A.

**Rollback test** (a slot that never becomes healthy):

```sh
ingest-slot clone
mount -o remount,rw /media/mmcblk*p1
mv /media/mmcblk*p1/slots/B/ingest.apkovl.tar.gz /media/mmcblk*p1/slots/B/broken
mount -o remount,ro /media/mmcblk*p1
ingest-slot try B --tries 1
reboot
```

Slot B cannot find its configuration, the initramfs gives up, the kernel
panics and restarts after 10 s.

- [ ] The following boot is on slot A again and `ingest-slot status` shows
      `"try_failed": "B"`. Clear it with `ingest-slot ack`.
- [ ] `ingest-slot rollback` now refuses to activate the broken slot B
      (`missing ingest.apkovl.tar.gz`). Repair it with `ingest-slot clone`.

**Watchdog test** (simulates a system that hangs):

```sh
pkill -9 -f /sbin/watchdog     # stop feeding the hardware watchdog
```

- [ ] The station resets by itself within about 10 minutes (the Intel TCO
      watchdog fires on its second timeout: up to 2 x 300 s) and boots
      normally.

## 5. Boot time

Please report:

- the time from power-on to the tty1 banner (stopwatch);
- the value in `cat /data/log/boot.log` (`boot OK ... at N s` after kernel
  start);
- `dmesg | grep -E 'ingest|Alpine Init'` (kernel timestamps).

Record the results in [boot-timing.md](boot-timing.md).

## 6. What to send back if something fails

- `ingest-install-*.log` from the stick;
- a photo of the screen;
- if the system boots: `dmesg > /data/log/dmesg.txt`, `logread >
  /data/log/messages.txt`, then boot the stick's *Rescue shell* to copy
  them, or read them on the console.

## Results on the Wyse 3040 (2026-09-26, version 0.1.0)

Run over SSH from the build PC unless noted.

| Test | Result |
|---|---|
| Installation from USB (F12 → *Boot from file*) | OK; reinstall over an existing system OK after the fix |
| eMMC detection (`mmcblk0`, boot0/boot1 excluded) | OK |
| Boot from eMMC through the registered UEFI entry | OK (`BootCurrent` = Ingest Station) |
| Reboot (dw_dmac quirk) | OK, several reboots |
| NTP and hardware clock | OK |
| Over-the-air update (`build.py --deploy`) | OK: 3 of 90 files sent, slot B confirmed |
| Rollback of a broken slot on trial | OK: kernel panic, back on the previous slot, `try_failed` set (~100 s) |
| Watchdog (ACPI WDAT) after its feeder is killed | OK: reset after 509 s, normal boot |
| Boot confirmed | 19.9-25.7 s after kernel start |
| `poweroff`, power cut while running | to be checked on site |
