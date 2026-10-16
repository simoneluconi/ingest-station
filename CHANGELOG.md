# Changelog

All notable changes to this project are documented here. The project
follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 3 (GUI on the monitor) and the phase 2 daemon.

### Added (phase 3: GUI)
- `ingest-gui`: full-screen touch interface on the monitor, a client of
  `ingestd`. pygame draws off-screen and frames are copied to a KMS/DRM dumb
  buffer (fbdev fallback): no X, no Mesa. Display hot-plug; input straight
  from evdev (keyboard, mouse, touch, numeric keypad; Italian and US
  layouts) with an always-visible focus ring and spatial arrow navigation.
- Light (default) and dark themes; Inter fonts (subset at build time from
  Alpine's `font-inter`); anti-aliased shapes and icons.
- Screens: home with one tile per source; source summary (new and already
  imported files, verification choice with time estimates, destination
  preview); progress with a step indicator; pause/retry when the share is
  lost; result ("Import complete" with the removal hint); import history.
- Settings: destination (NFS/SMB, server, share, credentials, folder
  browser, protocol version, connection test), import (auto-import and
  countdown, default verification, folder names, beeps), display (theme,
  keyboard layout), network (DHCP or static, QR code of the web address),
  web access (PIN, port), system (versions, rollback, restart, shut down,
  import history: rebuild from the share or forget all imports).
- First-boot wizard: share, connection test, destination folder.
- On-screen keyboard (letters, symbols, digits-only pad for PINs and ports)
  with a Cancel button for touch.
- USB link speed on every source ("5 Gbps", "480 Mbps"). "Slow link" is
  flagged when a USB 3 device (read from its BOS SuperSpeed capability) is
  plugged into a port that has carried a USB 3 link before, but linked
  slower: a USB 2 cable or a loose plug. Ports are learnt because firmware
  may describe a USB 2-only connector as USB 3 capable.
- One layout for every outcome (done, nothing new, ejected, errors) and
  centred dialogs; long texts shrink before being ellipsized.
- The boot is confirmed only once the GUI is running (heartbeat): a slot
  whose GUI does not start is rolled back.
- Daemon: `index.reset` and full `index.rebuild` commands, logs of device
  slots, volumes, removals and scan results.
- Development aids: `tools/wyse.py push` (copy the code into the running
  system), `tools/shot.sh` (send navigation commands to the GUI and fetch a
  screenshot over SSH).

### Changed (phase 3)
- Boot confirmed at about 19 s instead of 31 s once the GUI was required:
  i915 (~3.5 s) is loaded in the background by the initramfs while the
  packages are installed; unused drivers (sound, Intel ME, Dell WMI, power
  telemetry) are blacklisted; ingestd and the GUI start before the network;
  pygame's sound, MIDI and image libraries are no longer installed.
- GRUB: the embedded config of the ISO's `BOOTX64.EFI` no longer searches
  every disk for the ISO label ("error: no such device", a few seconds).
- The build refuses Python files that do not compile.

### Fixed (phase 3)
- After a cancelled or failed import the source stayed in error and a new
  import was refused: the source is now scanned again (files copied so far
  are recognised) without starting an automatic import.
- A cancelled import was shown as an error (red); it is now amber with
  "Files already copied are kept".
- "You can remove the card" was shown for cameras: "You can disconnect the
  camera".
- Dark halo around rounded corners, icons and the focus ring
  (anti-aliasing against transparent black).

- NFS: a protocol version the server accepts but whose reads fail (seen
  with NFS 4.2 on NFS-Ganesha 4.3: hash imports succeeded, full verification
  paused at the first read-back) is now skipped while negotiating: each
  mount is checked by writing a small file and reading it back directly from
  the server. The daemon renegotiates at start instead of reusing an old
  mount.
- NFS: ports 2049 and 111 are checked before mounting (3 s at most):
  "NFS 3 is not offered by ...", "... is not reachable" and unknown names
  are reported in seconds instead of 25-60 s of mount retries.
- Phones over MTP: the scan walked the whole phone (apps, caches, music) and
  stopped at the first folder Android refuses to list. Cameras and phones are
  now scanned in their media folders only (DCIM, Pictures, Movies, PRIVATE,
  AVCHD, MP_ROOT); unreadable folders are skipped. Phones show the model
  they report ("Samsung SM-S938B") and a phone icon.
- After an import, the source screen showed the summary from before it; it
  now shows the outcome of the last import.

### Measured on the Wyse 3040 (phase 3)
- Samsung Galaxy S25 Ultra over MTP (USB 3): 3017 photos and videos (80 GB)
  listed in 25 s.
- NFS 4.1 to a test server (NFS-Ganesha in Docker on a Windows PC): 501
  files, 495 MB in 17-20 s with hash verification, 32 s with full
  verification; checksums confirmed on the server with `xxhsum`.
- Canon EOS R6 over USB-C (PTP, 5 Gbit/s link): 41 files, 943.7 MB in 39 s
  (24 MB/s) to an SMB share; the files are recognised as imported at the
  next connection.

### Added (phase 2: daemon)
- `ingestd` daemon (OpenRC, restarted automatically), single source of
  truth, JSON-lines API on the Unix socket `/run/ingestd.sock` with a live
  state stream; `ingestctl` command line client.
- Source detection: USB disks, sticks and card readers (one disk per slot,
  empty slots shown), hot-plug through kernel uevents plus a 2 s rescan,
  coldplug at start. exFAT, FAT, NTFS (ntfs3), ext4 and HFS+; APFS and
  encrypted disks reported as not supported. Update and installer sticks
  are recognised.
- Read-only guarantee: the block device is set read-only in the kernel
  before mounting; ext4 is mounted with `noload`.
- Scan and recognition of already imported files by fingerprint (size +
  xxh128 of the first and last MiB); only files whose size was seen before
  are read, so new cards are summarised instantly.
- Copy engine: overlapped read/write with large reusable buffers,
  temporary file + fsync + atomic rename, mtime preserved, xxh128 while
  copying; up to 3 files flushed in parallel while the next one is written.
- Verification modes: none, hash (manifest `<folder>.xxh128`), full (the
  copies are read back from the share after dropping the caches).
- Destination folder `AAAA-MM-GG_HHMM_<label>`; when the card has no label,
  the camera brand is guessed from `DCIM` (100CANON, 100MSDCF...).
- SQLite index (WAL) on the data partition, one transaction per file,
  backed up to `<share>/.ingest-station/index.db` after every import.
- Destination: NFS or SMB on any server, protocol version negotiated
  automatically (NFS 4.2 → 3, SMB 3.1.1 → 2.1), soft mounts, health checks
  (read-only every 30 s, write probe before imports), folder listing.
- Share lost during a copy: the import pauses with the reason and waits for
  Retry (restarts from the interrupted file) or Cancel; no automatic retry.
- Card removed during an import: the import fails, completed files are
  kept; at the next insertion only the missing files are new.
- Optional auto-import with countdown (off by default), import queue,
  status beeps through the PC speaker (off by default).
- Cameras and phones over PTP/MTP through libgphoto2 (ctypes, no FUSE):
  one source per storage (e.g. the two SD slots of a camera), identified by
  model and USB serial; files are read with partial reads, so the fingerprint
  is the same as from a card reader (a file imported from the card is known
  when the camera is connected by cable, and vice versa). A disconnection or
  auto power-off ends the import; completed files are kept.
- `ingestctl rebuild-index`: rebuilds the index from the `.xxh128`
  manifests on the share (tested: 4 imports, 384 files in 44 s).
- The last import result stays in the state after the card is removed
  (`ingestctl dismiss`); clearer share errors; the share is shown offline
  while an import is paused.

### Measured on the Wyse 3040 (phase 2)
- 5.6 GB (183 files) from a USB 3 stick to an SMB share on a Windows PC with
  mandatory signing: 124 s, 45 MB/s average, CPU 93% idle. The stick reads
  at 107 MB/s; the share accepts ~58 MB/s.

## [0.1.0] - unreleased

Phase 1: build, installer and eMMC boot.

### Added
- `build.py`: builds the USB installer folder `dist/usb/` on Windows with
  Python only, from Alpine Linux 3.24.2 (kernel 6.18 LTS).
  - Verifies the Alpine ISO (SHA256 and GPG signature with the pinned
    release key), the package indexes (RSA signature) and every package
    (control and data hashes).
  - Resolves the package closure from the official signed APKINDEX and ships
    only the needed packages next to it (offline boot).
  - Pure-Python SquashFS, ISO9660 and cpio implementations.
- Diskless system with A/B slots on the eMMC ESP, selected by GRUB through
  grubenv with a 3-attempt trial and automatic rollback.
- `ingest-initrd.gz`: initramfs addition with the slot selection hook and
  an early hardware watchdog (iTCO).
- eMMC installer: robust disk detection, cancellable countdown, `wipefs`,
  write test for worn/read-only eMMC, verified copy, log on the USB stick.
- Wyse 3040 quirk fixes: eMMC drivers, `dw_dmac` blacklist, no console
  blanking, fallback EFI path, UUID-only references, card reader polling.
- `ingest-slot` command: status, trial, rollback, clone, boot confirmation.
- Console banner with version, slot and IP address; mDNS name
  `ingest.local`; DHCP with optional static address.
- `build.py --test-vm` and `tests/vm/qemu_vm.py`: smoke tests of the whole
  boot chain (installer, eMMC boot, A/B, rollback, watchdog) in QEMU.
- SSH server (OpenSSH, key-only root login). The build key is generated in
  `keys/private/` (never committed); extra public keys in
  `keys/authorized_keys` or `/data/ssh/authorized_keys`; persistent host key.
- Over-the-air updates: `build.py --deploy HOST` / `tools/wyse.py deploy`
  send only the changed files; the station completes, verifies and installs
  them in the inactive slot and boots it on trial (`ingest-slot stage-*`).
- The installer registers an "Ingest Station" UEFI boot entry (efibootmgr).
- NTP time is written to the hardware clock after every sync.

### Fixed (found in the VM before the first hardware test)
- Kernel panic during the boot media scan: the slot hook now runs before
  Alpine's init declares the scan failed.
- Watchdog auto-loaded with a 30 s heartbeat before the hook: the heartbeat
  is now a module parameter on the kernel command line.
- Missing `openssl` package (required by Alpine's init for the modloop
  signature).
- Partition nodes disappearing while the kernel re-reads the table during
  installation.
- `ingest-watchdog` overriding the initramfs timeout.
- `mkfs.ext4` zeroing the whole data partition on devices without TRIM.
- About 4 s per boot spent waiting in the boot media scan.

### Fixed (found on the Wyse 3040)
- Reinstalling over an existing installation failed ("Resource busy"): the
  modloop was taken from the installed eMMC instead of the USB stick. Only
  the boot media of the selected slot stays mounted now, and the installer
  stops before erasing anything if the eMMC is still in use.
- The watchdog was not armed in the initramfs: the Wyse exposes it through
  ACPI WDAT (`wdat_wdt`), now included.
- Boot confirmed at 19.9 s instead of 28.5 s: no per-boot modloop SHA1
  check, DHCP no longer blocks the boot.
- `ingest-bootok` was reported as crashed after finishing its job.
