# Architecture

## Constraints

- Build host: Windows with Python only (no WSL, Docker, VMs or native
  compilation).
- Target: Dell Wyse 3040 (Atom x5-Z8350, 2 GB RAM, 8 GB eMMC, UEFI only).
- Alpine Linux 3.24 in diskless mode: the root file system is a tmpfs
  rebuilt at every boot from the apkovl and the local package repository.

## Boot chain

```
UEFI firmware
  -> \EFI\BOOT\BOOTX64.EFI           GRUB 2.14 taken from the official Alpine ISO
  -> /boot/grub/grub.cfg             loads loadenv/probe from /boot/grub/x86_64-efi,
                                     reads grubenv, picks the slot
  -> /slots/<X>/vmlinuz-lts          Alpine linux-lts kernel from the ISO
     initrd: initramfs-lts           official Alpine initramfs (untouched)
             ingest-initrd.gz        our addition (see below)
  -> Alpine /init                    loads modules=..., scans for boot media,
                                     sources /ingest/early.sh (our hook)
  -> early.sh                        arms the iTCO watchdog, mounts the ESP by
                                     UUID, sets apkovl=/slots/<X>/ingest.apkovl.tar.gz
                                     and the repositories /slots/<X>/apks/{main,community}
  -> apk add (from local repo)       installs the packages into the tmpfs root
  -> OpenRC                          modloop (/slots/<X>/modloop-lts), services
```

The GRUB image is the ISO's `efi/boot/bootx64.efi`. It has most modules
built in but not `loadenv` (grubenv) and `probe` (UUID of the boot
partition); those are copied from the `grub-efi` package, and `build.py`
checks that the package is byte-identical to the modules embedded in the EFI
image (same build) before using them.

### ingest-initrd.gz

GRUB loads it after the official initramfs; the kernel unpacks both into the
same root, later files replacing earlier ones. It contains:

- `/init`: Alpine's init with a single line added after the boot media scan,
  `. /ingest/early.sh`. `build.py` fails if the anchor text is not found
  exactly once (new Alpine release: review the patch).
- `/ingest/early.sh`: slot selection and watchdog.
- the iTCO watchdog modules and the complete `modules.*` index from the
  modloop.
- `/etc/modprobe.d/ingest-wyse.conf` (dw_dmac blacklist).

Why the slot hook is needed: Alpine's init looks for package repositories
(`.boot_repository`) up to three levels deep on every mounted device, so with
two slots on the same partition it would mix the packages of both. Slots are
stored in `/slots/<X>/apks/<repo>/`, deeper than that search, and the hook
names the right one explicitly. An empty `/slots/.boot_repository` marker
lets the scan (`nlplug-findfs`) stop as soon as the boot partition appears
instead of waiting for its timeout (about 4 s saved per boot); the marker is
never used as a repository because the hook always sets the repositories.

## eMMC layout (GPT)

| # | Type | Size | Content |
|---|---|---|---|
| 1 | ESP, FAT32, label `INGEST_ESP` | 1.5 GiB | `EFI/BOOT/BOOTX64.EFI`, `boot/grub/{grub.cfg,grubenv,x86_64-efi/}`, `slots/A/`, `slots/B/` |
| 2 | ext4, label `ingest-data` | rest (~6 GB) | `/data`: `config/`, `index/`, `log/`, `keys/` |

A slot (~360 MB) contains `vmlinuz-lts`, `initramfs-lts`,
`ingest-initrd.gz`, `modloop-lts`, `ingest.apkovl.tar.gz`,
`apks/{main,community}/x86_64/` (official signed `APKINDEX.tar.gz` plus only
the needed `.apk` files), `boot/` (bootloader files for installation and
updates) and `slot.json` (version, packages and SHA256 of every file).

The data partition is never touched by updates. Configuration lives there,
not in the apkovl.

## A/B slots and rollback

grubenv keys: `active_slot` (known good), `try_slot` + `try_left` (update on
trial), `try_failed` (trial ran out of attempts), `data_uuid`.

1. An update writes the inactive slot, verifies it, then sets
   `try_slot=<new>`, `try_left=3` and reboots.
2. At each boot GRUB decrements `try_left` (it has no arithmetic: an explicit
   3 → 2 → 1 → 0 chain) and boots the trial slot while attempts remain.
3. When the system is healthy, `ingest-bootok` (`ingest-slot boot-ok`) sets
   `active_slot=<new>` and clears the trial. If the checks do not pass
   within 3 minutes during a trial, it reboots.
4. If the kernel hangs or cannot reach user space, the hardware watchdog
   armed by `early.sh` (300 s heartbeat, reset on the second expiry) resets
   the board; `panic=10` covers
   kernel panics.
5. When the attempts are exhausted, GRUB boots `active_slot` again and sets
   `try_failed`, which the UI reports.

GRUB also has a fallback menu entry for the other slot if the selected kernel
cannot be loaded at all.

grubenv is 1024 bytes and is always rewritten in place (same size, same
clusters) because GRUB's `save_env` writes the underlying disk blocks.

## Services (OpenRC)

| Runlevel | Services |
|---|---|
| sysinit | devfs, dmesg, mdev, hwdrivers, modloop |
| boot | modules, sysctl, hostname, bootmisc, syslog, hwclock, **ingest-watchdog**, **ingest-data** |
| default | **ingest-network**, **ingest-console**, avahi-daemon, ntpd, **ingest-bootok** |

Phase 2-4 add `ingestd` (daemon), `ingest-gui` and `ingest-web`.

## Build verification chain

1. ISO: SHA256 from the mirror and the GPG signature `.asc`, verified with
   the Alpine release key (fingerprint pinned in `buildlib/config.toml`,
   key in `keys/alpine-ncopa.asc`).
2. The Alpine package signing keys are taken from the verified ISO's
   initramfs.
3. APKINDEX: RSA/SHA1 signature checked against those keys.
4. Each `.apk`: SHA1 of the control segment must match the index `C:` field,
   and the data segment must match the SHA256 `datahash` of the control
   segment.

## Software architecture (phases 2-5)

- `ingestd`: single source of truth; device detection, queue, copy, verify,
  index (SQLite), share monitoring, updates, beeps. Exposes an HTTP +
  WebSocket API on 127.0.0.1 (the same on Windows in `--simulate`).
- `ingest-gui`: pygame (Alpine `py3-pygame` on the device, pygame-ce on
  Windows) rendering off-screen; output through KMS/DRM dumb buffers with
  pure-Python ioctls (no Mesa), input from evdev (touch, mouse, keyboard).
- `ingest-web`: aiohttp server on the LAN with WebSocket live updates;
  commands protected by a PIN.
