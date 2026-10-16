# Troubleshooting

## Wyse 3040 quirks

| Symptom | Cause and what the system does |
|---|---|
| Installers crash with `UnicodeDecodeError` | The eMMC reports a model name with an invalid byte (e.g. `MMC H8G4a\x92`). The installer never reads model names; the app decodes all device strings with replacement characters. |
| eMMC not detected by recent kernels | The `sdhci`, `sdhci-acpi` and `mmc_block` drivers are not auto-loaded. They are loaded explicitly: in the initramfs (`modules=` on the kernel command line; the Alpine ISO initramfs already contains them), in the installer and in the system (`/etc/modules`). If the eMMC disappears after a kernel update, the new slot does not boot and GRUB rolls back. |
| Firmware does not boot the installed system | The bootloader is always installed at the fallback path `\EFI\BOOT\BOOTX64.EFI` (the 3040 often ignores NVRAM entries) and an "Ingest Station" UEFI entry is also registered. In the BIOS, check *Boot Sequence*; if needed add a boot option pointing to `\EFI\BOOT\BOOTX64.EFI` on the eMMC. |
| USB stick listed in F12 but does not boot | Use F12 → *Boot from file* and select `EFI\BOOT\BOOTX64.EFI`. Check that Secure Boot is disabled. |
| Wrong time | The clock is synchronised with NTP (`pool.ntp.org`) and written to the hardware clock after every sync, so it survives power cuts. |
| The date goes back (e.g. to 2018) at every power-on | The CMOS battery of the unit is flat: the hardware clock loses the time when the power is removed. NTP corrects it a few seconds after the network comes up; replace the CR2032 battery to keep the right time from the first second. |
| A USB 3 stick shows *480 Mbps · slow link* | The stick and the port support USB 3 but the link came up at USB 2 speed: USB 2 cable or extension, plug not fully inserted, dirty contacts. A stick in a USB 2-only port is not flagged. |
| Leftover ThinOS/ThinLinux partitions | The installer runs `wipefs` on every partition and on the whole disk before partitioning. |
| Hang on shutdown or reboot | Caused by the `dw_dmac` DMA driver: it is blacklisted (`/etc/modprobe.d/ingest-wyse.conf` and `modprobe.blacklist=` on the kernel command line). |
| Monitor does not wake up after standby | Console blanking and display power saving are disabled (`consoleblank=0`); the GUI never blanks the screen. |
| eMMC read-only | Probably worn out. The installer checks the `ro`/`force_ro` flags and does a write/read-back test, then stops with a clear message instead of failing half way. |
| Wrong disk | No fixed device names are used: the target is the only non-removable disk with `device/type == MMC`, excluding `mmcblk*boot0/boot1/rpmb` and the USB stick. Partitions are referenced by UUID only. |
| The station hangs during boot | A hardware watchdog (ACPI WDAT on the Wyse 3040, Intel TCO elsewhere) is armed in the initramfs and then fed by the `ingest-watchdog` service: if the system hangs, the board resets within about 10 minutes (the TCO fires on its second 300 s timeout) and GRUB counts the attempt of a slot on trial. A kernel panic reboots after 10 s. |

## Share problems

| Symptom | What to check |
|---|---|
| *cannot write to the share* (NFS) | Root squashing: allow root or map all users to an account that can write (see [destinations.md](destinations.md#nfs-what-the-server-must-allow)). |
| *NFS 3 is not offered by ...*, *... is not reachable* | NFS ports 2049 and 111 are checked before mounting: the server does not offer that NFS version, or is not reachable at that address. |
| A server misbehaves with the negotiated protocol version | Force a version in *Settings → Destination*. |

## Useful commands

On the console (tty2, `Alt+F2`, login `root`) or over SSH (see
[remote-access.md](remote-access.md)):

| Command | Shows |
|---|---|
| `ingest-slot status` | state of the A/B slots |
| `dmesg \| grep ingest` | kernel messages from the boot scripts |
| `cat /data/log/boot.log` | boot confirmations |
| `logread` | system log |
| `ingestctl status` | daemon state: sources, share, current import |

Logs: the daemon in `/data/log/ingestd.log`, GUI tracebacks in
`/var/log/ingest-gui.log`, the installation in `/data/log/install.log` (and
on the USB stick).
