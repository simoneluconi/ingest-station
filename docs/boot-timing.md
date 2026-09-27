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

## Measurements

| Date | Version | Boot confirmed | Notes |
|---|---|---|---|
| 2026-09-26 | 0.1.0 (first install) | 28.5 s | first hardware boot |
| 2026-09-26 | 0.1.0 (OTA update) | 19.9 s | modloop verify off, DHCP in background |
| 2026-09-27 | 0.1.0 + GUI (OTA) | 31.3 s | GUI heartbeat now required; GUI after the network |
| 2026-09-27 | 0.1.0 + GUI (OTA) | 19.6 s | i915 in the background, GUI before the network, GRUB search removed |
