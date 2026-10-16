# Remote access (SSH) and over-the-air updates

After the first installation from USB, the station can be reached and
updated from the build PC over the LAN: no more USB stick.

## SSH keys

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
- **The station's own identity** (host key) is kept on its data partition,
  so it does not change at every boot. It does change after a reinstall:
  delete the station's line from `keys/private/known_hosts` (used by
  `tools/wyse.py`) or from `~/.ssh/known_hosts` (used by `ssh`).

### Adding your own keys

For another PC, a phone app, etc.:

1. Create a key, if you do not have one:
   `ssh-keygen -t ed25519`. On Windows the public key is in
   `C:\Users\<you>\.ssh\id_ed25519.pub`; on Linux/macOS in
   `~/.ssh/id_ed25519.pub`.
2. Copy the **public** key line (it starts with `ssh-ed25519`) into
   `keys/authorized_keys`, one key per line. Create the file if needed; it
   only contains public keys, so it can safely be committed.
3. Rebuild and update the station (`build.py --deploy`, below).

Alternatively, keys appended on the station to `/data/ssh/authorized_keys`
survive reboots and updates.

## Connecting

The IP address is shown on the console banner and in the GUI's status bar;
the station also answers to `ingest.local` (mDNS).

```powershell
# with the tool in this repository (no ssh client needed)
.venv\Scripts\python tools\wyse.py status 192.168.1.50
.venv\Scripts\python tools\wyse.py sh 192.168.1.50 "dmesg | grep ingest"

# or with the SSH client of Windows / Git
ssh -i keys\private\ssh_ed25519 root@192.168.1.50
```

If the Windows `ssh` client refuses the key with *UNPROTECTED PRIVATE KEY
FILE*, restrict its permissions once:

```powershell
icacls keys\private\ssh_ed25519 /inheritance:r /grant:r "$($env:USERNAME):R"
```

Keep SSH on the local network only: do not forward port 22 on the router.

On the station, `ingestctl` controls the daemon from the command line
(`ingestctl status`, `ingestctl share set`, `ingestctl rebuild-index`...;
`ingestctl -h` lists the commands).

## Updating over the network

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

How the A/B slots work: [usage.md](usage.md#ab-slots-and-rollback) and
[architecture.md](architecture.md#ab-slots-and-rollback).
