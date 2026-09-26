import pytest

from ingest.common import grubenv


def test_roundtrip_and_size(tmp_path):
    p = tmp_path / "grubenv"
    grubenv.create(str(p), {"active_slot": "A", "data_uuid": "1234"})
    data = p.read_bytes()
    assert len(data) == 1024
    assert data.startswith(b"# GRUB Environment Block\nactive_slot=A\ndata_uuid=1234\n#")
    assert grubenv.read(str(p)) == {"active_slot": "A", "data_uuid": "1234"}


def test_write_in_place(tmp_path):
    p = tmp_path / "grubenv"
    grubenv.create(str(p), {"active_slot": "A"})
    grubenv.write(str(p), {"active_slot": "B", "try_slot": "B", "try_left": "3"})
    assert grubenv.read(str(p))["try_left"] == "3"
    assert p.stat().st_size == 1024


def test_refuses_wrong_size(tmp_path):
    p = tmp_path / "grubenv"
    p.write_bytes(b"# GRUB Environment Block\n")
    with pytest.raises(grubenv.GrubEnvError):
        grubenv.write(str(p), {"a": "b"})


def test_overflow_and_invalid():
    with pytest.raises(grubenv.GrubEnvError):
        grubenv.serialize({"k": "x" * 2000})
    with pytest.raises(grubenv.GrubEnvError):
        grubenv.serialize({"a=b": "c"})
    with pytest.raises(grubenv.GrubEnvError):
        grubenv.parse(b"garbage")


def test_parse_grub_written_block():
    # GRUB's save_env output with an emptied variable
    block = b"# GRUB Environment Block\ntry_slot=\ntry_failed=B\n"
    block += b"#" * (1024 - len(block))
    assert grubenv.parse(block) == {"try_slot": "", "try_failed": "B"}
