# Boot timing

Target: from kernel start to the GUI in the shortest possible time.

## Estimate before measurements (phase 1)

| Stage | Estimate |
|---|---|
| Firmware + GRUB | device dependent, not counted |
| Kernel + initramfs unpack | 3-4 s |
| Boot media scan, modloop | 1-2 s |
| `apk add` into RAM (~70 packages, ~54 MiB in phase 1) | 5-10 s |
| OpenRC up to `ingest-bootok` | ~3 s |

The package installation at every boot is the largest cost of diskless
mode; it grows with the GUI packages (phase 3, pygame + SDL + numpy).

## Measurements

| Date | Version | Power-on to banner | `boot OK at` (kernel time) | Notes |
|---|---|---|---|---|
| | 0.1.0 | | | |
