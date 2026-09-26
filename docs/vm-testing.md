# Smoke tests in a QEMU virtual machine

> QEMU was used during phase 1 and then uninstalled from the build PC: since
> the station has SSH and over-the-air updates, tests run directly on the
> Wyse. The harness is kept for boot-chain changes that would otherwise need
> a USB reinstall to recover from.

The Wyse 3040 remains the reference, but most of the boot chain can be
exercised on the Windows build host with QEMU before each hardware test.

## Setup

- QEMU for Windows (`winget install SoftwareFreedomConservancy.QEMU`), no
  WSL needed. Without Windows Hypervisor Platform QEMU runs in software
  emulation (TCG): slow but sufficient.
- `python build.py --test-vm` builds `dist/usb-vm/`, identical to
  `dist/usb/` except for test-only kernel parameters:
  - console on the serial port (`console=ttyS0`), no `quiet`;
  - watchdog heartbeat 60 s instead of 300 s;
  - installer: countdown 2 s, log mirrored on the serial port, and the
    emulated eMMC accepted although QEMU reports it as removable
    (`ingest_test_target=mmcblk0`).
- `tests/vm/qemu_vm.py` drives the VM: q35 chipset (ICH9 with the iTCO
  watchdog), OVMF UEFI firmware, 2 GB RAM, an emulated eMMC on an SDHCI
  controller, the USB folder as a read-only USB stick (QEMU vvfat).

```powershell
python tests\vm\qemu_vm.py start --usb dist\usb-vm --fresh   # install run
python tests\vm\qemu_vm.py stop                              # always stop cleanly
python tests\vm\qemu_vm.py start --sata                      # boot the installed disk
python tests\vm\qemu_vm.py sh "ingest-slot status"           # root shell on the serial console
python tests\vm\qemu_vm.py shot screen.png                   # screenshot of tty1
```

## Differences from the Wyse

| Topic | VM | Wyse 3040 |
|---|---|---|
| Booting the installed eMMC | OVMF has no SD/MMC driver: the installed image is re-attached as a SATA disk (`--sata`). The system finds its partitions by UUID, so this also checks that no device names are hard-coded. | Firmware boots the eMMC directly. |
| eMMC host | `sdhci-pci`, device reported as removable | `sdhci-acpi`, non-removable |
| Flushes | The emulated eMMC sends no cache flushes: stop QEMU with `stop` (monitor `quit`), never kill it, or the image can lose recent writes. | Real eMMC honours flushes. |
| `mkfs.ext4` | No TRIM: formatting is slow in the VM. | TRIM supported. |
| USB stick | Read-only (vvfat): the installer cannot save its log there. | Writable. |
| dw_dmac, audio, display | Not emulated. | Hardware test only. |

## Results (version 0.1.0)

Verified in the VM:

- GRUB from the Alpine ISO loads `loadenv`/`probe` from `$prefix` and reads
  the boot partition UUID; A/B selection and `save_env` on FAT work.
- The initramfs hook finds the boot partition by UUID, selects the slot,
  arms the watchdog; the 70 packages install from the slot repository.
- Installer: eMMC detection (boot0/boot1 excluded), countdown, `wipefs`,
  write test, GPT, formatting, verified copy, reboot.
- First boot from the installed disk: modloop, data partition by UUID,
  console banner with IP, boot confirmation.
- A/B: `ingest-slot clone`, `try B`, boot of B and confirmation.
- Rollback: a broken slot on trial panics, GRUB returns to the previous slot
  and sets `try_failed`; manual rollback to a broken slot is refused.
- Watchdog: after its feeder is killed the TCO watchdog resets the VM.
- SSH (key only, password refused) and an over-the-air update: 3 of 90
  files sent, staged slot verified, trial boot confirmed.

Not visible in the VM and found on the Wyse: the watchdog is exposed through
ACPI WDAT, not iTCO; reinstalling over an existing installation (the VM
always installed on an empty disk).

Bugs found by the VM and fixed before any hardware test: kernel panic in
the boot media scan (hook ran too late), watchdog auto-loaded with a 30 s
heartbeat before the hook, missing `openssl` package, partition nodes
disappearing during formatting, watchdog service overriding the timeout,
eager ext4 initialisation writing the whole partition, ~4 s wasted per boot
in the media scan.
