import gzip
import stat

import pytest

from buildlib import cpio, initrd


def test_cpio_roundtrip():
    w = cpio.Writer()
    w.file("usr/lib/modules/x/a.ko", b"\x7fELF", 0o644)
    w.symlink("lib", "usr/lib")
    w.file("init", b"#!/bin/sh\n", 0o755)
    entries = {e.name: e for e in cpio.read_entries(gzip.decompress(w.finish_gzip()))}
    assert stat.S_ISDIR(entries["usr/lib/modules/x"].mode)
    assert entries["usr/lib/modules/x/a.ko"].data == b"\x7fELF"
    assert stat.S_ISLNK(entries["lib"].mode) and entries["lib"].data == b"usr/lib"
    assert entries["init"].mode & 0o777 == 0o755


def test_patch_init():
    init = 'x\n\t$repoopts -a "$ROOT"/tmp/apkovls\neend $?\n# Setup network\n'
    out = initrd.patch_init(init)
    # the hook runs before eend, which would otherwise drop to the recovery shell
    assert out.index(". /ingest/early.sh") < out.index("eend $ingest_rc") < out.index("# Setup network")
    assert "eend $?" not in out
    with pytest.raises(initrd.InitrdError):
        initrd.patch_init("something else")
    with pytest.raises(initrd.InitrdError):
        initrd.patch_init(init + init)


def test_module_closure():
    dep = ("kernel/drivers/watchdog/iTCO_wdt.ko: kernel/drivers/mfd/intel_pmc_bxt.ko "
           "kernel/drivers/watchdog/iTCO_vendor_support.ko\n"
           "kernel/drivers/watchdog/iTCO_vendor_support.ko:\n"
           "kernel/drivers/mfd/intel_pmc_bxt.ko:\n"
           "kernel/drivers/mmc/host/sdhci-acpi.ko: kernel/drivers/mmc/host/sdhci.ko\n"
           "kernel/drivers/mmc/host/sdhci.ko:\n")
    assert initrd.module_closure(dep, ["iTCO_wdt"]) == [
        "kernel/drivers/mfd/intel_pmc_bxt.ko",
        "kernel/drivers/watchdog/iTCO_vendor_support.ko",
        "kernel/drivers/watchdog/iTCO_wdt.ko"]
    # '-' and '_' are interchangeable in module names
    assert "kernel/drivers/mmc/host/sdhci.ko" in initrd.module_closure(dep, ["sdhci_acpi"])
    with pytest.raises(initrd.InitrdError):
        initrd.module_closure(dep, ["nope"])
