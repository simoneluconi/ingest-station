"""Integration test against the real Alpine modloop (needs the cached ISO)."""

import glob
import os

import pytest

from buildlib.iso9660 import ISO9660
from buildlib.squashfs import SquashFS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISOS = sorted(glob.glob(os.path.join(ROOT, "cache", "alpine-standard-*-x86_64.iso")))


@pytest.mark.skipif(not ISOS, reason="Alpine ISO not in cache (run build.py first)")
def test_read_modules_from_modloop():
    with ISO9660(ISOS[-1]) as iso:
        off, _ = iso.offset("/boot/modloop-lts")
        sq = SquashFS(iso.f, off)
        kver = next(k for k in sq.listdir(sq.lookup("/modules")) if k[0].isdigit())
        dep = sq.read_file(sq.lookup(f"/modules/{kver}/modules.dep")).decode()
        assert "kernel/drivers/mmc/host/sdhci-acpi.ko:" in dep
        ko = sq.read_file(sq.lookup(f"/modules/{kver}/kernel/drivers/watchdog/iTCO_wdt.ko"))
        assert ko[:4] == b"\x7fELF"
