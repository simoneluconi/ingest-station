# Boot timing

Target: from kernel start to the GUI in the shortest possible time.

## Dell Wyse 3040, version 0.1.0 (phase 1, no GUI yet)

Kernel timestamps (`dmesg`) after the phase 1 optimisations:

| Stage | Time since kernel start |
|---|---|
| Alpine init starts | 1.4 s |
| Boot partition found, watchdog armed | 3.9-4.3 s |
| 72 packages installed into RAM | 7.4 s |
| Modloop mounted | 8.3 s |
| Hardware drivers loaded (i915 at ~17.8 s before the changes) | ~13 s |
| Boot confirmed (`/data/log/boot.log`) | **19.9 s** |
| Ethernet link up (switch negotiation) | 21.2 s |

Before the optimisations the boot was confirmed at 28.5 s. Changes:

- `modloop_verify=no`: Alpine's init computed the SHA1 of the 300 MB
  modloop at every boot (~3.5 s). Slots are verified with SHA-256 when they
  are installed or updated instead.
- DHCP no longer blocks the boot: one quick attempt, then `udhcpc` keeps
  trying in the background (the link comes up ~20 s after power-on).
- An empty `/slots/.boot_repository` marker lets the boot media scan stop as
  soon as the ESP appears (~4 s saved).

## Dell Wyse 3040, version 0.1.0 with the GUI (phase 3)

The boot is confirmed only once the GUI is running (heartbeat), so the
figure now includes the GUI start. Timeline after the phase 3 changes:

| Stage | Time since kernel start |
|---|---|
| Watchdog armed, slot found | 6.0 s |
| Packages installed into RAM (109) | 6.4-10.1 s |
| i915 (display) loaded, in the background during the install | 7.5 s |
| `hwdrivers` coldplug | 13.4-16.1 s |
| ingestd and GUI started | 17.4-17.5 s |
| Boot confirmed | **19.6 s** |

Changes:

- GRUB: the embedded config of the ISO's `BOOTX64.EFI` searched every disk
  for the ISO label (a few seconds, then "error: no such device"). The build
  replaces that line with a no-op of the same length (not a comment: that
  config runs in GRUB's minimal parser).
- i915 alone took ~3.5 s inside `hwdrivers`. It is now in the extra initrd
  and `early.sh` loads it in the background while the packages are
  installed; `/init` waits for it before `switch_root`. An initramfs-only
  blacklist keeps the boot media scan from loading it synchronously.
- Unused drivers are blacklisted (`/etc/modprobe.d/ingest-unused.conf`:
  sound, Intel ME, Dell WMI, power telemetry).
- ingestd and the GUI start before the network (the daemon follows address
  changes by itself); DHCP messages go to syslog.
- pygame's optional sound, MIDI and image libraries are not installed
  (4 packages fewer).

How to measure: a temporary `/etc/rc.conf.d/zz-boottrace.conf` with
`echo "rc-trace: $RC_SVCNAME $RC_CMD $(cut -d' ' -f1 /proc/uptime)" > /dev/kmsg`
logs every OpenRC service start to `dmesg`.

Next candidates: the two coldplug passes of `hwdrivers` (~2.7 s) and `mdev`
(~1.5 s); the package install (~3.7 s).

## Dell Wyse 3040, fast boot (phase 3, October 2026)

From power-on (firmware times from the ACPI FPDT table, the rest from
`dmesg` and `/data/log/boot.log`):

| Stage | Time |
|---|---|
| Firmware (POST) until GRUB starts | 6.9 s |
| GRUB loads kernel and initramfs, kernel takes over | 10.05 s (GRUB 3.1 s) |
| System restored from the boot cache | kernel + 3.7 s |
| First frame of the GUI on screen | kernel + **7.2 s** (17.3 s from power-on) |
| Boot confirmed | kernel + **7.6 s** |

Changes:

- **Boot cache.** The first boot after an install or update saves the
  installed root filesystem as an uncompressed tar in `/data/cache`, keyed
  by the overlay, the package indexes, the kept services and the kernel.
  Later boots unpack it (~1.2 s) instead of running `apk add` (~4.1 s). A
  missing, stale or broken cache falls back to the normal install.
- **No boot media scan on eMMC boots.** `nlplug-findfs` probed every disk
  and waited for USB; when GRUB passes `ingest_slot=` the ESP is mounted
  directly (the scan still runs if that fails, and on the USB installer).
- **Slim initramfs.** Alpine's initramfs carries the boot drivers of every
  machine (22.5 MB, read by GRUB through the firmware at ~10 MB/s). The
  build drops families that cannot matter on a thin client or small PC
  (server SCSI/RAID HBAs, InfiniBand, NVMe over fabrics, storage targets,
  10G NICs, software RAID: `initramfs_drop` in `buildlib/config.toml`) and
  the firmware nobody asks for: 12.6 MB, GRUB 4.0 -> 3.1 s. SATA, NVMe,
  every USB host and storage driver, MMC/SD, HID and virtual machine
  drivers stay, so similar hardware still boots.
- **Parallel OpenRC** (`rc_parallel`) and only the services in use: the
  other init scripts are moved away before OpenRC builds its dependency
  tree. ingestd and the GUI are in the `boot` runlevel; the boot
  confirmation no longer waits for the network.
- **Drivers in the background.** i915 loads in the initramfs during the
  media search (`nls_utf8` preloaded so the FAT mount never waits behind
  it). `ingest-devices` replaces mdev/hwdrivers: the hotplug handler is set
  at once, the network card driver is loaded first, the coldplug of the
  rest waits until the GUI shows its first frame.
- **Precompiled Python** (checked-hash `.pyc`) when the build machine runs
  the same Python minor version as the station.

Not in our hands: the firmware's 6.9 s (Dell BIOS: Fastboot "Minimal", PXE
and unused devices off shorten it) and the kernel image itself (14 MB).

How to measure: `ingest-gui` writes "first frame on screen" to `dmesg`; the
boot confirmation goes to `/data/log/boot.log`; the firmware times are in
the ACPI FPDT table (`/sys/firmware/acpi/tables/FPDT`, records read from
`/dev/mem`). The first boot after an update is slower (~13 s) because it
rebuilds the boot cache.

## Measurements

| Date | Version | Boot confirmed | Notes |
|---|---|---|---|
| 2026-09-26 | 0.1.0 (first install) | 28.5 s | first hardware boot |
| 2026-09-26 | 0.1.0 (OTA update) | 19.9 s | modloop verify off, DHCP in background |
| 2026-09-27 | 0.1.0 + GUI (OTA) | 31.3 s | GUI heartbeat now required; GUI after the network |
| 2026-09-27 | 0.1.0 + GUI (OTA) | 19.6 s | i915 in the background, GUI before the network, GRUB search removed |
| 2026-10-16 | 0.1.0 + GUI (OTA) | 7.6 s | boot cache, no media scan, parallel OpenRC, slim initramfs; first frame 7.2 s |
