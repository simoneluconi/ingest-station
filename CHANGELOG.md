# Changelog

All notable changes to this project are documented here. The project
follows [Semantic Versioning](https://semver.org/).

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
