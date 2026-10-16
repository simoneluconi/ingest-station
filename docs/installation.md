# Installation

Installing Ingest Station takes three steps: prepare the Wyse 3040 BIOS,
build the USB installer on a Windows PC, then boot the Wyse from the stick
and install to the internal eMMC. After that the stick is needed only to
reinstall: updates go over the network (see
[remote-access.md](remote-access.md)).

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

Requirements:

- Python 3.12+ (tested with 3.14) and an internet connection.
- Recommended: [Git for Windows](https://git-scm.com/download/win). Its
  `gpg` is used to verify the Alpine signature; without it, pass `--no-gpg`
  and only the SHA256 checksums are verified.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python build.py
```

The build downloads the official Alpine ISO and packages into `cache/`,
verifies them and produces **`dist/usb/`** (about 360 MB):

- ISO: SHA256 and GPG signature of the Alpine release key;
- package index: Alpine RSA signature;
- each package: control and data hashes.

Later builds reuse the cache; `--offline` builds without network access.
The first build also creates the SSH key used for remote access (see
[remote-access.md](remote-access.md#ssh-keys)).

| Option | Effect |
|---|---|
| `--offline` | use only the download cache |
| `--no-gpg` | allow building without `gpg` (SHA256 only) |
| `--deploy HOST` | after building, update the station at `HOST[:PORT]` over SSH ([details](remote-access.md#updating-over-the-network)) |
| `--test-vm` | build `dist/usb-vm/` for QEMU smoke tests ([vm-testing.md](vm-testing.md)) |

## 3. Create the USB stick and install

1. Format a USB stick as **FAT32** (Windows: right click → Format → FAT32,
   or `format X: /FS:FAT32 /Q` for sticks up to 32 GB; larger sticks need a
   third-party tool or a smaller partition).
2. Copy the **content** of `dist/usb/` to the root of the stick, so that
   `EFI\BOOT\BOOTX64.EFI` is at the top level. No Rufus, Etcher or image
   writer is needed.
3. Plug the stick into the Wyse, power on and press **F12**. If choosing the
   USB stick in the list does not start it, use **Boot from file** (or
   *UEFI boot path*) and select `EFI\BOOT\BOOTX64.EFI` on the stick.
4. In the menu choose **Install Ingest Station to eMMC** (the default after
   3 s).
5. The installer finds the internal eMMC and shows
   `Installing to eMMC 7.8 GB (mmcblk0)` with a **10-second countdown:
   press any key to cancel**. Then it erases the eMMC, creates the
   partitions, copies the system and verifies it.
   - If no suitable eMMC is found, **nothing is written**: the disks seen
     are listed on screen and in the log.
   - If the eMMC turns out to be read-only (worn out), the installer stops
     and says so.
6. At the end, **remove the USB stick** and press a key: the station
   restarts from the eMMC. The installer registers an **Ingest Station**
   UEFI boot entry first in the boot order, and the bootloader is also at
   the fallback path `\EFI\BOOT\BOOTX64.EFI`.

On the first boot, a short wizard on the monitor asks for the network share:
see [destinations.md](destinations.md) for what the server must allow, and
[usage.md](usage.md) for daily use.

### Logs, console and rescue

- The installation log is always saved on the stick
  (`ingest-install-YYYYMMDD-HHMMSS.log`) and on the station
  (`/data/log/install.log`).
- Console login: user `root`, no password (physical access only). Remote
  access uses SSH with keys only, see [remote-access.md](remote-access.md).
- The USB menu also offers a **Rescue shell** that boots the system from the
  stick without writing anything (log in as `root`, no password).

If something goes wrong, see [troubleshooting.md](troubleshooting.md).
