from ingest.common import blockdev

from sysfs_fixture import disk, wyse_3040


def test_partition_name_suffix():
    assert blockdev.partition_name("mmcblk0", 1) == "mmcblk0p1"
    assert blockdev.partition_name("nvme0n1", 2) == "nvme0n1p2"
    assert blockdev.partition_name("sda", 1) == "sda1"


def test_wyse_emmc_is_the_only_candidate(tmp_path):
    wyse_3040(str(tmp_path))
    disks = blockdev.list_disks(str(tmp_path))
    names = [d.name for d in disks]
    assert "loop0" not in names
    cands, rejected = blockdev.install_candidates(disks, {"sda"})
    assert [d.name for d in cands] == ["mmcblk0"]
    reasons = {d.name: why for d, why in rejected}
    assert reasons["mmcblk0boot0"] == "eMMC hardware partition"
    assert reasons["mmcblk0rpmb"] == "eMMC hardware partition"
    assert reasons["sda"] == "boot device"
    emmc = cands[0]
    assert emmc.partitions == ["mmcblk0p1", "mmcblk0p2", "mmcblk0p3"]
    assert 7.8 < emmc.size_gb < 7.9


def test_non_utf8_model_is_not_read_by_default(tmp_path):
    wyse_3040(str(tmp_path))
    emmc = [d for d in blockdev.list_disks(str(tmp_path)) if d.name == "mmcblk0"][0]
    assert emmc.model == ""


def test_non_utf8_model_decodes_tolerantly(tmp_path):
    wyse_3040(str(tmp_path))
    disks = {d.name: d for d in blockdev.list_disks(str(tmp_path), read_model=True)}
    assert disks["mmcblk0"].model == "MMC H8G4a�"
    assert disks["sda"].model == "Ultra Fit"
    assert "�" in disks["mmcblk0"].describe()


def test_no_emmc_found(tmp_path):
    disk(str(tmp_path), "sda", 60062500, removable=1, partitions=["sda1"])
    disk(str(tmp_path), "sdb", 0, removable=1)  # empty card reader slot
    cands, rejected = blockdev.install_candidates(blockdev.list_disks(str(tmp_path)), {"sda"})
    assert cands == []
    assert {d.name for d, _ in rejected} == {"sda", "sdb"}


def test_sd_card_in_mmc_slot_is_rejected(tmp_path):
    disk(str(tmp_path), "mmcblk1", 62333952, dev_type="SD")
    cands, rejected = blockdev.install_candidates(blockdev.list_disks(str(tmp_path)), set())
    assert cands == []
    assert "not MMC" in rejected[0][1]


def test_removable_or_small_emmc_is_rejected(tmp_path):
    disk(str(tmp_path), "mmcblk0", 15269888, removable=1, dev_type="MMC")
    disk(str(tmp_path), "mmcblk1", 1000, dev_type="MMC")
    cands, rejected = blockdev.install_candidates(blockdev.list_disks(str(tmp_path)), set())
    assert cands == []
    assert dict((d.name, w) for d, w in rejected)["mmcblk0"] == "removable"


def test_read_only_flags(tmp_path):
    disk(str(tmp_path), "mmcblk0", 15269888, dev_type="MMC", ro=1)
    assert blockdev.list_disks(str(tmp_path))[0].read_only


def test_parent_disk(tmp_path):
    wyse_3040(str(tmp_path))
    assert blockdev.parent_disk("sda1", str(tmp_path)) == "sda"
    assert blockdev.parent_disk("mmcblk0p2", str(tmp_path)) == "mmcblk0"
    assert blockdev.parent_disk("sda", str(tmp_path)) == "sda"


def test_mounts_parsing(tmp_path):
    p = tmp_path / "mounts"
    p.write_bytes(b"/dev/sda1 /media/sda1 vfat ro,noatime 0 0\n"
                  b"/dev/sdb1 /media/My\\040Card vfat ro 0 0\n"
                  b"/dev/sdc1 /media/bad\xff exfat ro 0 0\n")
    m = blockdev.mounts(str(p))
    assert m[0] == ("/dev/sda1", "/media/sda1", "vfat", "ro,noatime")
    assert m[1][1] == "/media/My Card"
    assert m[2][1] == "/media/bad�"
