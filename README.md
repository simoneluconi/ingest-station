# Ingest Station

Turns a **Dell Wyse 3040** thin client into a dedicated ingest station: plug
in a card reader, a camera or a USB disk and its content is copied, verified
and indexed onto a network share (NFS or SMB), with a touch/keyboard GUI on
the monitor and a web UI on the LAN.

The system is a minimal **Alpine Linux** running entirely from RAM
(diskless mode), so it is robust against power cuts and barely writes to the
internal eMMC. It is built on Windows with Python only.

> **Status: phase 1** (build, installer, eMMC boot with A/B slots, hardware
> quirks). The ingest daemon, GUI, web UI and signed updates arrive in the
> next phases, see [CHANGELOG.md](CHANGELOG.md) and
> [docs/phase1-testing.md](docs/phase1-testing.md).

---

## 1. Prepare the Wyse 3040 BIOS

> **Power supply warning:** the Wyse 3040 exists in a **5 V** version and
> a **12 V** version. Check the rating on the unit's label and use only the
> matching power supply: swapping them can destroy the device.

1. Power on and press **F2** repeatedly to enter the BIOS setup.
2. The default administrator password is **`Fireport`** (on some units
   **`Fireport2`**).
3. If the password was changed and is unknown: unplug the power, open the
   case, locate the **PWCLR** jumper/button near the coin battery, keep it
   pressed (or jumpered) while powering on, then power off and restore it.
   The BIOS password is cleared.
4. In the setup:
   - **General → Boot Sequence**: boot mode **UEFI** (legacy is not
     supported by the 3040 anyway).
   - **System Configuration → USB Configuration**: enable **Boot from USB**.
   - **Secure Boot → Secure Boot Enable**: **disabled**.
   - Optional: **Power Management → AC Recovery: Power On**, so the station
     starts by itself when power returns.
5. Save and exit. **F12** at power-on opens the one-time boot menu.

## 2. Build the USB installer (Windows)

Requirements: Python 3.12+ (tested with 3.14) and an internet connection.
[Git for Windows](https://git-scm.com/download/win) is recommended: its `gpg`
is used to verify the Alpine signature (without it, pass `--no-gpg` and only
SHA256 checksums are verified).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python build.py
```

The build downloads the official Alpine ISO and packages into `cache/`,
verifies them (ISO: SHA256 + GPG signature of the Alpine release key;
package index: Alpine RSA signature; each package: control and data hashes)
and produces **`dist/usb/`** (about 360 MB). Later builds reuse the cache;
`--offline` builds without network access.

Run the tests with `.venv\Scripts\python -m pytest`.

## 3. Create the USB stick and install

1. Format a USB stick as **FAT32** (Windows: right click → Format → FAT32,
   or `format X: /FS:FAT32 /Q` for sticks up to 32 GB; larger sticks need a
   third-party tool or a smaller partition).
2. Copy the **content** of `dist/usb/` to the root of the stick (so that
   `EFI\BOOT\BOOTX64.EFI` is at the top level). No Rufus/Etcher/image
   writer is needed.
3. Plug the stick into the Wyse, power on, press **F12** and choose the USB
   stick. Choose **Install Ingest Station to eMMC** (default after 3 s).
4. The installer finds the internal eMMC and shows
   `Installing to eMMC 7.8 GB (mmcblk0)` with a **10-second countdown:
   press any key to cancel**. Then it erases the eMMC, creates the
   partitions, copies the system and verifies it.
   - If no suitable eMMC is found, **nothing is written**: the disks seen
     are listed on screen and in the log.
   - If the eMMC turns out to be read-only (worn out), the installer stops
     and says so.
5. At the end, **remove the USB stick** and press a key: the station
   restarts from the eMMC. The stick is needed again only to reinstall.

The installation log is always saved on the stick
(`ingest-install-YYYYMMDD-HHMMSS.log`) and on the station
(`/data/log/install.log`).

The USB menu also offers a **Rescue shell** that boots the system from the
stick without writing anything (log in as `root`, no password).

## 4. Configure the share on the NAS (Unraid)

The destination (NFS or SMB server, share, folder, credentials) is
configured **from the station's GUI**: a first-boot wizard, then
*Settings → Destination*, with a *Test connection* button and a folder
browser (phase 3). On the NAS side, create a dedicated share, e.g.
`Ingest`. Enabling the Unraid **cache pool** for that share makes ingest
faster (the mover transfers the files to the array later).

**NFS (default, lowest CPU usage on the Wyse)**

1. *Settings → NFS*: **Enable NFS: Yes**, *Max server protocol version*:
   **4** (Unraid 6.10 or later).
2. *Shares → Ingest → NFS Security Settings*: **Export: Yes**,
   **Security: Private**, rule:
   `192.168.1.50(sec=sys,rw,all_squash,anonuid=99,anongid=100)`
   (replace the IP with the station's address; files will belong to
   `nobody:users`, like the ones created over SMB).
3. On the station: server = NAS address, path = `/mnt/user/Ingest`.

**SMB (alternative)**

1. *Settings → SMB*: **Enable SMB: Yes**.
2. *Users*: create a user, e.g. `ingest`, with a password.
3. *Shares → Ingest → SMB Security Settings*: **Export: Yes**,
   **Security: Private**, `ingest` = **Read/Write**.
4. The Wyse CPU is weak: keep SMB **signing not required** and
   **encryption off** for this share (Unraid defaults). Do not add
   `server signing = mandatory` or `smb encrypt = required` in
   *SMB Extras*.
5. On the station: server = NAS address, share = `Ingest`, user/password.
   The credentials are stored on the station's data partition with
   permissions `600`.

## 5. Daily use, updates and rollback

*(Complete description in phase 2-5.)* Plug in a card: the summary shows
what is new and what was already imported; choose *Import new only*
(default), *Import all* or *Cancel/Eject*. The final screen turns
**green** ("You can remove the card") or **red** with the error.

**A/B slots.** The eMMC holds two complete systems, slot A and slot B.
An update is written to the inactive slot and started "on trial": if it
does not report a healthy boot within 3 attempts, the station automatically
goes back to the previous slot. A manual rollback is available in the
settings (and, for now, with `ingest-slot rollback` on the console).

## 6. Troubleshooting (Wyse 3040 quirks)

| Symptom | Cause and what the system does |
|---|---|
| Installers crash with `UnicodeDecodeError` | The eMMC reports a model name with an invalid byte (e.g. `MMC H8G4a\x92`). The installer never reads model names; the app decodes all device strings with replacement characters. |
| eMMC not detected by recent kernels | The `sdhci`, `sdhci-acpi` and `mmc_block` drivers are not auto-loaded. They are loaded explicitly: in the initramfs (`modules=` on the kernel command line; the Alpine ISO initramfs already contains them), in the installer and in the system (`/etc/modules`). If the eMMC disappears after a kernel update, the new slot does not boot and GRUB rolls back. |
| Firmware does not boot the installed system | The 3040 ignores NVRAM boot entries: the bootloader is always installed at the fallback path `\EFI\BOOT\BOOTX64.EFI`. Check that the eMMC is enabled in the BIOS boot sequence. |
| Leftover ThinOS/ThinLinux partitions | The installer runs `wipefs` on every partition and on the whole disk before partitioning. |
| Hang on shutdown or reboot | Caused by the `dw_dmac` DMA driver: it is blacklisted (`/etc/modprobe.d/ingest-wyse.conf` and `modprobe.blacklist=` on the kernel command line). |
| Monitor does not wake up after standby | Console blanking and display power saving are disabled (`consoleblank=0`); the GUI never blanks the screen. |
| eMMC read-only | Probably worn out. The installer checks the `ro`/`force_ro` flags and does a write/read-back test, then stops with a clear message instead of failing half way. |
| Wrong disk | No fixed device names are used: the target is the only non-removable disk with `device/type == MMC`, excluding `mmcblk*boot0/boot1/rpmb` and the USB stick. Partitions are referenced by UUID only. |
| The station hangs during boot | A hardware watchdog (Intel TCO) is armed in the initramfs: after 5 minutes without a healthy system the station resets, and GRUB counts the attempt of a slot on trial. |

Useful console commands (tty2, `Alt+F2`, login `root`): `ingest-slot status`,
`dmesg | grep ingest`, `cat /data/log/boot.log`, `logread`.

## Repository layout

| Path | Content |
|---|---|
| `build.py` | builds `dist/usb/` (later also the signed update package) |
| `buildlib/` | pure-Python ISO9660/SquashFS readers, cpio writer, APKINDEX resolver and verification, apkovl builder, boot templates, `config.toml` |
| `src/ingest/` | the application (shared code in `common/`) |
| `overlay/` | system files placed in the apkovl (OpenRC services, configuration) |
| `installer/` | eMMC installer |
| `tests/` | unit tests (sysfs fixtures, grubenv, slots, packages, initrd) |
| `docs/` | architecture, test procedures, boot timings |
| `keys/` | Alpine release GPG key; the private update key is never committed |
