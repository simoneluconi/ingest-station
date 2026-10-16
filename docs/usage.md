# Daily use

The station is used from the monitor, with a touchscreen, a mouse or a
keyboard: arrows move the focus, Enter activates, Esc goes back; a numeric
keypad works too. The first time, a short wizard asks for the network share
(see [destinations.md](destinations.md)).

## Importing

### 1. Plug in a source

A card reader, a USB stick or disk, or a camera/phone with its cable.
Cameras and phones are read over PTP/MTP: on a phone choose *File transfer*.
Several sources at once are fine: the home screen shows one tile each.

<table>
  <tr>
    <td><img src="images/home.png" width="400" alt="Home with two sources"></td>
    <td><img src="images/phone.png" width="400" alt="A phone over MTP"></td>
  </tr>
</table>

Each tile shows the USB link speed ("5 Gbps", "480 Mbps"). *Slow link*
means a USB 3 device that linked at USB 2 speed in a USB 3 port: usually a
USB 2 cable or a loose plug.

### 2. Check the summary

![Source summary](images/source.png)

The summary shows:

- how many files are new, and which were imported before (when and into
  which folder);
- the destination folder that will be created;
- the **verification**:
  - *None*: fastest, copy only;
  - *Hash*: a checksum (xxh128) of every file while copying, saved next to
    the copies; recommended;
  - *Full*: the copies are read back from the share.

### 3. Import

Press **Import new files** (or *Import all*). The progress screen shows the
steps, speed, time left and destination. You can go back to the home screen
and queue another source: it starts when the current one ends.

![Import progress](images/progress.png)

### 4. Result

The result says **Import complete** and *You can remove the card* (or
*You can disconnect the camera*), or why it failed. Files already copied are
always kept: the next time only the missing ones count as new.

![Import complete](images/result.png)

## Other situations

- **Share lost during a copy**: the import pauses with the reason and waits
  for *Retry* or *Cancel*.
- **Eject** releases a card without importing.
- **Card removed or camera disconnected during an import**: the import
  fails, completed files are kept; at the next connection only the missing
  files are new.

## Settings

![Settings](images/settings.png)

- *Settings → Destination*: NFS/SMB server, share, credentials, folder,
  protocol version, connection test.
- *Settings → Import*: automatic import (with a countdown), default
  verification, folder names, beeps.
- *Settings → Display*: theme (light or dark), keyboard layout.
- *Settings → Network*: DHCP or static address.
- *Settings → System*: versions, rollback, restart, shut down, and
  *Import history*, which rebuilds the list of imported files from the
  folders on the share, or forgets it (every file then counts as new;
  nothing is deleted on the share).

## A/B slots and rollback

The eMMC holds two complete systems, slot A and slot B. An update is written
to the inactive slot and started "on trial": if it does not report a healthy
boot within 3 attempts, the station automatically goes back to the previous
slot. The boot counts as healthy only once the GUI is running.

A manual rollback is available in *Settings → System* (and with
`ingest-slot rollback` on the console). `ingest-slot status` shows the
state of both slots.

Updates themselves are sent over the network: see
[remote-access.md](remote-access.md#updating-over-the-network). The boot
chain and the slot logic are described in
[architecture.md](architecture.md#ab-slots-and-rollback).
