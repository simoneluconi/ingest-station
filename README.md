# Ingest Station

Turns a **Dell Wyse 3040** thin client into a dedicated ingest station: plug
in a card reader, a camera or a USB disk and its content is copied, verified
and indexed onto a network share (NFS or SMB), with a touch/keyboard GUI on
the monitor and a web UI on the LAN.

The system is a minimal **Alpine Linux** running entirely from RAM
(diskless mode), so it is robust against power cuts and barely writes to the
internal eMMC. It is built on Windows with Python only.

> **Status: phase 3** — build, installer, eMMC boot with A/B slots, the
> ingest daemon (cards, USB disks, cameras and phones; NFS/SMB) and the GUI
> on the monitor work on the Wyse 3040. The web UI (phase 4) and signed
> update packages (phase 5) come next, see [CHANGELOG.md](CHANGELOG.md).

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

Run the tests with `.venv\Scripts\python -m pytest`. The boot chain can
also be smoke-tested in a QEMU virtual machine, see
[docs/vm-testing.md](docs/vm-testing.md).

## 3. Create the USB stick and install

1. Format a USB stick as **FAT32** (Windows: right click → Format → FAT32,
   or `format X: /FS:FAT32 /Q` for sticks up to 32 GB; larger sticks need a
   third-party tool or a smaller partition).
2. Copy the **content** of `dist/usb/` to the root of the stick (so that
   `EFI\BOOT\BOOTX64.EFI` is at the top level). No Rufus/Etcher/image
   writer is needed.
3. Plug the stick into the Wyse, power on, press **F12**. If choosing the
   USB stick in the list does not start it, use **Boot from file** (or
   *UEFI boot path*) and select `EFI\BOOT\BOOTX64.EFI` on the stick.
   In the menu choose **Install Ingest Station to eMMC** (default after 3 s).
4. The installer finds the internal eMMC and shows
   `Installing to eMMC 7.8 GB (mmcblk0)` with a **10-second countdown:
   press any key to cancel**. Then it erases the eMMC, creates the
   partitions, copies the system and verifies it.
   - If no suitable eMMC is found, **nothing is written**: the disks seen
     are listed on screen and in the log.
   - If the eMMC turns out to be read-only (worn out), the installer stops
     and says so.
5. At the end, **remove the USB stick** and press a key: the station
   restarts from the eMMC. The installer registers an **Ingest Station**
   UEFI boot entry first in the boot order, and the bootloader is also at
   the fallback path `\EFI\BOOT\BOOTX64.EFI`. The stick is needed again only
   to reinstall.

At the first log in on the console: user `root`, no password (console
access only; there is no remote shell).

The installation log is always saved on the stick
(`ingest-install-YYYYMMDD-HHMMSS.log`) and on the station
(`/data/log/install.log`).

The USB menu also offers a **Rescue shell** that boots the system from the
stick without writing anything (log in as `root`, no password).

## 4. Remote access (SSH) and updates over the network

After the first installation from USB, the station can be reached and
updated from the build PC over the LAN: no more USB stick.

### SSH keys

Access is **by key only**: password logins are disabled.

- **The build key is created automatically.** The first run of `build.py`
  generates an Ed25519 key pair in `keys/private/ssh_ed25519` (private) and
  `keys/private/ssh_ed25519.pub` (public). The public key is put in the
  image as root's authorised key; the private key stays on the PC and is
  never committed (`keys/private/` is in `.gitignore`). Later builds reuse
  the same key.
- **Back up `keys/private/`.** If the private key is lost, the station only
  trusts the old one: a new key is generated at the next build and must be
  installed from the USB stick once.
- **Adding your own keys** (another PC, a phone app...):
  1. Create a key, if you do not have one. Windows (PowerShell):
     `ssh-keygen -t ed25519` → the public key is in
     `C:\Users\<you>\.ssh\id_ed25519.pub`. Linux/macOS: same command,
     `~/.ssh/id_ed25519.pub`.
  2. Copy the **public** key line (starts with `ssh-ed25519`) into
     `keys/authorized_keys`, one key per line (create the file; it only
     contains public keys, so it can safely be committed).
  3. Rebuild and update the station (`build.py --deploy`, below).

  Alternatively, keys appended on the station to `/data/ssh/authorized_keys`
  survive reboots and updates.
- **The station's own identity** (host key) is kept on its data partition,
  so it does not change at every boot. It does change after a reinstall:
  delete the station's line from `keys/private/known_hosts` (used by
  `tools/wyse.py`) or from `~/.ssh/known_hosts` (used by `ssh`).

### Connecting

The IP address is shown on the console banner (or use `ingest.local`).

```powershell
# with the tool in this repository (no ssh client needed)
.venv\Scripts\python tools\wyse.py status 192.168.1.50
.venv\Scripts\python tools\wyse.py sh 192.168.1.50 "dmesg | grep ingest"

# or with the SSH client of Windows / Git
ssh -i keys\private\ssh_ed25519 root@192.168.1.50
```

If the Windows `ssh` client refuses the key with *UNPROTECTED PRIVATE KEY
FILE*, restrict its permissions once:
`icacls keys\private\ssh_ed25519 /inheritance:r /grant:r "$($env:USERNAME):R"`.

Keep SSH on the local network only: do not forward port 22 on the router.

### Updating over the network

```powershell
.venv\Scripts\python build.py --deploy 192.168.1.50
```

1. `build.py` builds the new system as usual.
2. It asks the station which files the running slot contains and sends
   **only the files that changed** (usually a few MB: the kernel and the
   modules rarely change).
3. The station completes the new system with its own unchanged files,
   verifies every checksum, installs it into the **inactive slot** and
   reboots into it **on trial**.
4. `build.py` waits for the station to come back: `update confirmed` when
   the new system is healthy; if not, the station automatically goes back to
   the previous slot within 3 attempts and `build.py` reports
   `UPDATE ROLLED BACK`.

`tools\wyse.py deploy 192.168.1.50 --no-reboot` stages the update and
leaves the reboot to you. An update is refused while a previous one is
still on trial.

## 5. Configure the destination (NAS or server)

Any **NFS** or **SMB** server works: a NAS (Synology, QNAP, TrueNAS, Unraid,
Asustor...), a Windows PC, a Linux server. The destination (type, server,
share, folder, credentials) is configured **from the station's GUI**: a
first-boot wizard, then *Settings → Destination*, with *Test connection* and
a folder browser (or over SSH with `ingestctl share set`).

General advice:

- Create a **dedicated share or folder** for the imports.
- Give the station a **fixed IP address** (DHCP reservation on the router,
  or a static address in the settings): NFS permissions refer to it.
- **NFS** is recommended: it needs the least CPU on the Wyse, so copies are
  faster. **SMB** is simpler where users and passwords are already managed.
- The protocol version is **negotiated automatically** (NFS 4.2 → 4.1 →
  4.0 → 3, SMB 3.1.1 → 3.0 → 2.1) and can be forced in the settings if a
  server misbehaves. SMB1 is not supported.
- If the NAS has an SSD cache, enable it for this share: imports finish
  sooner.

### NFS: what the server must allow

1. Enable the NFS service (version 4 if the server offers it).
2. Export the share **read-write** to the station's IP address.
3. **Root squashing**: the station writes as `root`. Either allow it
   (`no_root_squash`, "No mapping", "Maproot: root") or map every client
   user to an account that can write in the folder (`all_squash` with
   `anonuid`/`anongid`, "Map all users to admin", "Mapall user").
   With the default root squash and no write permission for `nobody`,
   the station reports *cannot write to the share*.
4. On the station: server = NAS address, path = the exported path.

### SMB: what the server must allow

1. Enable SMB 2 or later.
2. Create a user for the station with **read/write** access to the share.
3. Signing and encryption work but cost CPU on the Wyse: if the server
   lets you, leave them optional for this share.
4. On the station: server = address or name, share = share name,
   user/password (stored on the station with permissions `600`), domain only
   for Windows domain accounts.

### Examples

| Server | NFS | SMB |
|---|---|---|
| **Synology DSM** | *Control Panel → File Services → NFS*: enable (max NFSv4.1). *Shared Folder → Edit → NFS Permissions → Create*: host = station IP, *Read/Write*, *Squash: Map all users to admin*, *Security: sys*. Path: `/volume1/<share>`. | *File Services → SMB* (minimum SMB2). User with *Read/Write* on the shared folder. |
| **QNAP QTS** | *Network & File Services → NFS Service*: enable v4. *Shared Folders → Edit Shared Folder Permission → NFS host access*: station IP, *read/write*, squash *NO_ROOT_SQUASH* or all users. Path: as shown by the NAS for the export (e.g. `/<share>`). | *Win/Mac/NFS → Microsoft Networking*, minimum SMB2; user with RW. |
| **TrueNAS** | *Shares → NFS → Add*: path `/mnt/<pool>/<dataset>`, host = station IP, *Maproot User: root* (or *Mapall* to the dataset owner); enable the NFS service with NFSv4. | *Shares → SMB → Add* on the dataset; user with write ACL. |
| **Unraid** | *Settings → NFS*: enable, max version 4. *Shares → <share> → NFS*: Export *Yes*, Security *Private*, rule `IP(sec=sys,rw,all_squash,anonuid=99,anongid=100)`. Path: `/mnt/user/<share>`. | *Settings → SMB*: enable. *Users*: create a user; *Shares → <share> → SMB*: Security *Private*, user *Read/Write*. |
| **Windows** | (not built in) | Folder *Properties → Sharing → Advanced Sharing*: share it, permissions *Change* for a local user (a Microsoft account also works, user = e-mail address); the NTFS permissions must allow writing. |
| **Linux** | `/etc/exports`: `/srv/ingest IP(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)`, then `exportfs -ra`. | Samba share with `read only = no` and a user added with `smbpasswd -a`. |

## 6. Daily use, updates and rollback

The station is used from the monitor, with a touchscreen, a mouse or a
keyboard (arrows move the focus, Enter activates, Esc goes back; a numeric
keypad works too). The first time, a short wizard asks for the network share.

1. **Plug in** a card reader, a USB stick or disk, or a camera/phone with
   its cable (cameras and phones over PTP/MTP: on a phone choose *File
   transfer*). Several sources at once are fine: the home screen shows one
   tile each, with the USB link speed ("5 Gbps", "480 Mbps"; *slow link*
   means a USB 3 device that linked at USB 2 speed in a USB 3 port, usually
   a USB 2 cable or a loose plug).
2. The **summary** shows how many files are new and which were imported
   before (when and into which folder), the destination folder that will be
   created, and the **verification**: *None* (fastest), *Hash* (a checksum
   of every file while copying, saved next to the copies; recommended) or
   *Full* (the copies are read back from the share).
3. **Import new files** (or *Import all*). The progress screen shows the
   steps, speed, time left and destination; you can go back to the home
   screen and queue another source: it starts when the current one ends.
4. The result says **Import complete** and *You can remove the card* (or
   *disconnect the camera*), or why it failed. Files already copied are
   always kept: the next time only the missing ones count as new.

If the network share goes away during a copy, the import pauses with the
reason and waits for *Retry* or *Cancel*. *Eject* releases a card without
importing. *Settings → Import* enables the automatic import (with a
countdown) and sets the default verification and the folder names.
*Settings → System → Import history* rebuilds the list of imported files
from the folders on the share, or forgets it (every file then counts as
new; nothing is deleted on the share).

**A/B slots.** The eMMC holds two complete systems, slot A and slot B.
An update is written to the inactive slot and started "on trial": if it
does not report a healthy boot within 3 attempts, the station automatically
goes back to the previous slot. A manual rollback is available in the
settings (and, for now, with `ingest-slot rollback` on the console).

## 7. Troubleshooting (Wyse 3040 quirks)

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
| `keys/` | Alpine release GPG key; `keys/private/` (never committed) holds the SSH key and, later, the update signing key |
| `tools/wyse.py` | SSH client for the station: commands, status, over-the-air updates, `push` of the code into the running system |
| `tools/shot.sh` | drives the GUI over SSH and fetches a screenshot (see `docs/gui.md`) |
