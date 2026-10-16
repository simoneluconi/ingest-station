# Development

The build runs on Windows with Python only (no WSL, Docker, VMs or native
compilation); the code is tested on the Wyse itself over SSH. See
[architecture.md](architecture.md) for how the pieces fit together.

## Repository layout

| Path | Content |
|---|---|
| `build.py` | builds `dist/usb/` (later also the signed update package) |
| `buildlib/` | pure-Python ISO9660/SquashFS readers, cpio writer, APKINDEX resolver and verification, apkovl builder, boot templates, `config.toml` |
| `src/ingest/` | the application: `daemon/` (`ingestd`), `gui/` (`ingest-gui`), `ctl.py` (`ingestctl`), shared code in `common/` |
| `overlay/` | system files placed in the apkovl (OpenRC services, configuration) |
| `installer/` | eMMC installer |
| `tests/` | unit tests (sysfs fixtures, grubenv, slots, packages, initrd, devices, importer...), `nfs-server/` (test NFS server), `vm/` (QEMU harness) |
| `docs/` | user guides, architecture, test procedures, boot timings |
| `keys/` | Alpine release GPG key; `keys/private/` (never committed) holds the SSH key and, later, the update signing key |
| `tools/wyse.py` | SSH client for the station: commands, status, over-the-air updates, `push` of the code into the running system |
| `tools/shot.sh` | drives the GUI over SSH and fetches a screenshot |

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python build.py
```

Build options and the first installation: [installation.md](installation.md).

## Tests

```powershell
.venv\Scripts\python -m pytest
```

The unit tests run on Windows. The boot chain can also be smoke-tested in a
QEMU virtual machine ([vm-testing.md](vm-testing.md)); the hardware test
checklist of phase 1 is in [phase1-testing.md](phase1-testing.md).

## Working on the station

`tools/wyse.py` talks to the station over SSH with the build key (see
[remote-access.md](remote-access.md)):

```powershell
.venv\Scripts\python tools\wyse.py status ingest.local            # slot status
.venv\Scripts\python tools\wyse.py sh ingest.local "logread"      # run a command
.venv\Scripts\python tools\wyse.py deploy ingest.local            # OTA update from dist/usb
.venv\Scripts\python tools\wyse.py push ingest.local --restart ingest-gui
```

- `push` copies the code into the running system only (RAM): a reboot
  restores the installed version. `build.py --deploy HOST` installs it for
  good, in the inactive slot with automatic rollback.
- `tools/shot.sh OUT.png [command ...]` sends navigation commands to the GUI
  and downloads a screenshot (host from `$WYSE`, default `ingest.local`):

  ```sh
  sh tools/shot.sh work/home.png
  sh tools/shot.sh work/s.png screen:settings cat:system
  ```

The GUI internals, its layout rules and the full list of `shot.sh` commands
are in [gui.md](gui.md).

## Test NFS server

[tests/nfs-server/](../tests/nfs-server/README.md) contains an NFSv4 server
(NFS-Ganesha in Docker, also on Docker Desktop for Windows) for testing
without a NAS:

```sh
docker build -t ingest-nfs-test tests/nfs-server
docker run -d --name ingest-nfs --privileged -p 2049:2049 -v ingest-nfs:/export ingest-nfs-test
```

On the station: *Settings → Destination*, NFS, server = the PC's address,
export path `/ingest`. The README there explains how to check the copies on
the server side with `xxhsum`.

## Conventions

- All files (code, UI texts, docs, changelog) are in English.
- Files that end up on the station keep LF line endings (`.gitattributes`).
- Changes are recorded in [CHANGELOG.md](../CHANGELOG.md).
