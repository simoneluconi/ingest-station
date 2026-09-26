# Boot timing

Target: from kernel start to the GUI in the shortest possible time.

## Dell Wyse 3040, version 0.1.0 (phase 1, no GUI yet)

Kernel timestamps (`dmesg`) after the phase 1 optimisations:

| Stage | Time since kernel start |
|---|---|
| Alpine init starts | 1.4 s |
| Boot partition found, watchdog armed | 3.9-4.3 s |
| 72 packages installed into RAM | 7.4 s |
| Modloop mounted | 8.3 s |
| Hardware drivers loaded (i915 at ~17.8 s before the changes) | ~13 s |
| Boot confirmed (`/data/log/boot.log`) | **19.9 s** |
| Ethernet link up (switch negotiation) | 21.2 s |

Before the optimisations the boot was confirmed at 28.5 s. Changes:

- `modloop_verify=no`: Alpine's init computed the SHA1 of the 300 MB
  modloop at every boot (~3.5 s). Slots are verified with SHA-256 when they
  are installed or updated instead.
- DHCP no longer blocks the boot: one quick attempt, then `udhcpc` keeps
  trying in the background (the link comes up ~20 s after power-on).
- An empty `/slots/.boot_repository` marker lets the boot media scan stop as
  soon as the ESP appears (~4 s saved).

Next candidates (phase 3, with the GUI): replace the generic hardware
driver coldplug (`hwdrivers`, ~5 s) with an explicit module list, start the
GUI before the network services.

## Measurements

| Date | Version | Boot confirmed | Notes |
|---|---|---|---|
| 2026-09-26 | 0.1.0 (first install) | 28.5 s | first hardware boot |
| 2026-09-26 | 0.1.0 (OTA update) | 19.9 s | modloop verify off, DHCP in background |
