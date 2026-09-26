# Changelog

All notable changes to this project are documented here. The project
follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 2 (daemon), first part: USB mass storage.

### Added
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

### Measured on the Wyse 3040
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
