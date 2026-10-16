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


SWITCH = 'exec switch_root $switch_root_opts $sysroot $chart_init "$KOPT_init" $KOPT_init_args\n'


def test_patch_init():
    init = ('x\nebegin "Mounting boot media"\n' + initrd.SCAN + 'eend $?\n# Setup network\n'
            + initrd.INSTALL_ANCHOR + "eend 0\n"
            "if x; then\n\t" + SWITCH + "fi\n" + SWITCH)
    out = initrd.patch_init(init)
    # the hook runs before eend, which would otherwise drop to the recovery shell
    assert out.index(". /ingest/early.sh") < out.index("eend $ingest_rc") < out.index("# Setup network")
    assert "eend $?" not in out
    # the generic scan is skipped on eMMC boots (ingest_slot= without
    # ingest_mode=), kept for the installer/rescue and as a fallback
    hook = out[out.index("ingest_scan=yes"):out.index("eend $ingest_rc")]
    assert hook.count("nlplug-findfs") == 2
    assert '*" ingest_mode="*) ;;' in hook and '*" ingest_slot="*) ingest_scan= ;;' in hook
    assert hook.index('if [ -n "$ingest_scan" ]') < hook.index(". /ingest/early.sh") \
        < hook.index("# fallback")
    # boot cache: tried before the package install, saved after it, else unchanged
    restore = out.index("ingest_cache_restore")
    assert restore < out.index("apk add") < out.index("ingest_cache_save") < out.index("eend 0")
    assert out.count("apk add --root") == 2       # the two original branches, once each
    # the background drivers are waited for before the final switch_root only
    wait = 'wait "$ingest_bg_pid"'
    assert out.count(wait) == 1
    assert out.index("\t" + SWITCH) < out.index(wait) < out.rindex(SWITCH)
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


def test_slim():
    md = "usr/lib/modules/6.1-lts/"
    dep = ("kernel/drivers/scsi/sd_mod.ko: kernel/drivers/scsi/scsi_mod.ko\n"
           "kernel/drivers/scsi/scsi_mod.ko:\n"
           "kernel/drivers/scsi/qla2xxx/qla2xxx.ko: kernel/drivers/scsi/scsi_mod.ko\n"
           "kernel/drivers/usb/storage/usb-storage.ko: kernel/drivers/scsi/scsi_mod.ko\n"
           "kernel/drivers/usb/storage/ums-eneub6250.ko: kernel/drivers/usb/storage/usb-storage.ko\n")
    reg = stat.S_IFREG | 0o644
    entries = [cpio.Entry(md + "modules.dep", reg, dep.encode()),
               cpio.Entry("usr/lib/firmware", stat.S_IFDIR | 0o755)]
    for line in dep.splitlines():
        rel = line.split(":")[0]
        fw = {"qla2xxx.ko": b"\0firmware=ql2400_fw.bin\0",
              "ums-eneub6250.ko": b"\0firmware=ene-ub6250/sd_init1.bin\0"}
        entries.append(cpio.Entry(md + rel, reg, fw.get(rel.rsplit("/", 1)[1], b"")))
    for name in ["ql2400_fw.bin.zst", "ene-ub6250/sd_init1.bin.zst"]:
        entries.append(cpio.Entry("usr/lib/firmware/" + name, reg, b"fw"))

    out, removed = initrd.slim(entries, "6.1-lts", ["drivers/scsi"], ["sd-mod"])
    names = {e.name.rsplit("/", 1)[1] for e in out}
    # dropped family, except what is kept or needed by a kept module
    assert removed == 1 and "qla2xxx.ko" not in names
    assert {"sd_mod.ko", "scsi_mod.ko", "usb-storage.ko"} <= names
    # firmware only for the modules that are left
    assert "sd_init1.bin.zst" in names and "ql2400_fw.bin.zst" not in names
    # the result can be written back
    back = list(cpio.read_entries(gzip.decompress(initrd.write_entries(out))))
    assert [e.name for e in back if e.name in {x.name for x in out}] == [x.name for x in out]
