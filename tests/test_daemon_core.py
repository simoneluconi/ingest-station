import datetime as dt
import os
import threading

import pytest

from ingest.daemon import copier, fingerprint, naming
from ingest.daemon.index import Index


# -- naming -------------------------------------------------------------

def test_sanitize():
    assert naming.sanitize('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert naming.sanitize("name. ") == "name"
    assert naming.sanitize("CON.txt") == "_CON.txt"
    assert naming.sanitize("") == "_"
    assert naming.sanitize("bad\x01name�") == "bad_name�"
    long = "x" * 300 + ".CR3"
    s = naming.sanitize(long)
    assert s.endswith(".CR3") and len(s.encode()) <= naming.MAX_NAME


def test_sanitize_relpath():
    assert naming.sanitize_relpath("DCIM\\100CANON/IMG:1.CR3") == "DCIM/100CANON/IMG_1.CR3"
    assert naming.sanitize_relpath("../../etc/passwd") == "etc/passwd"


def test_folder_name_and_label():
    when = dt.datetime(2026, 9, 26, 14, 5)
    assert naming.folder_name(when, "EOS_R6") == "2026-09-26_1405_EOS_R6"
    assert naming.folder_name(when, "X", "{year}/{label}") == "2026_X"  # '/' sanitised
    assert naming.folder_name(when, "X", "{bogus}") == "2026-09-26_1405_X"
    assert naming.source_label("NO NAME", "Canon EOS R6") == "Canon_EOS_R6"
    assert naming.source_label("", "") == "Card"
    assert naming.source_label("", "MassStorageClass", hint="Sony") == "Sony"
    assert naming.source_label("EOS_DIGITAL", "SD/MMC", hint="Canon") == "EOS_DIGITAL"
    assert naming.source_label("", "USB Storage") == "Card"
    assert naming.dcim_brand(["100MSDCF"]) == "Sony"
    assert naming.dcim_brand(["MISC", "100CANON"]) == "Canon"
    assert naming.dcim_brand(["DJI_001"]) == "DJI"
    assert naming.dcim_brand(["100MLT33"]) == ""
    taken = {"a", "a_2"}
    assert naming.unique_folder("a", taken.__contains__) == "a_3"


# -- fingerprint --------------------------------------------------------

@pytest.mark.parametrize("size", [0, 10, fingerprint.EDGE, fingerprint.EDGE + 7,
                                  2 * fingerprint.EDGE - 1, 2 * fingerprint.EDGE,
                                  3 * fingerprint.EDGE + 12345])
def test_streamed_fingerprint_matches_file(tmp_path, size):
    data = os.urandom(size)
    p = tmp_path / "f"
    p.write_bytes(data)
    col = fingerprint.EdgeCollector(size)
    for i in range(0, size, 777_777):
        col.update(data[i:i + 777_777])
    assert col.fingerprint() == fingerprint.of_file(str(p))
    assert fingerprint.size_of(col.fingerprint()) == size


def test_fingerprint_ignores_name_but_not_content(tmp_path):
    a, b = tmp_path / "IMG_0001.CR3", tmp_path / "IMG_9999.CR3"
    data = os.urandom(3 << 20)
    a.write_bytes(data)
    b.write_bytes(data)
    assert fingerprint.of_file(str(a)) == fingerprint.of_file(str(b))
    b.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    assert fingerprint.of_file(str(a)) != fingerprint.of_file(str(b))


# -- copier -------------------------------------------------------------

def make_item(tmp_path, rel, size):
    src = tmp_path / "src" / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(os.urandom(size))
    os.utime(src, (1_700_000_000, 1_700_000_000))
    return copier.Item(str(src), rel, size, 1_700_000_000.0)


@pytest.mark.parametrize("size", [0, 1000, 3 * 1024 * 1024 + 5])
def test_copy_file(tmp_path, size):
    item = make_item(tmp_path, "DCIM/100CANON/IMG_0001.CR3", size)
    seen = []
    res = copier.copy_file(item, str(tmp_path / "dst"), progress=seen.append, chunk=1 << 20)
    out = tmp_path / "dst" / "DCIM" / "100CANON" / "IMG_0001.CR3"
    assert out.read_bytes() == open(item.src, "rb").read()
    assert int(out.stat().st_mtime) == 1_700_000_000
    assert sum(seen) == size
    assert res.fp == fingerprint.of_file(item.src)
    assert res.xxh128 == copier.hash_file(str(out))
    assert not list((tmp_path / "dst").rglob("*" + copier.TMP_SUFFIX))


def test_copy_source_missing_is_source_error(tmp_path):
    item = copier.Item(str(tmp_path / "nope"), "a.jpg", 10, 0)
    with pytest.raises(copier.SourceError):
        copier.copy_file(item, str(tmp_path / "dst"))
    assert not list((tmp_path / "dst").rglob("*" + copier.TMP_SUFFIX))


def test_copy_dest_unwritable_is_dest_error(tmp_path):
    item = make_item(tmp_path, "a.jpg", 10)
    blocker = tmp_path / "dst"
    blocker.write_bytes(b"a file where a folder should be")
    with pytest.raises(copier.DestError):
        copier.copy_file(item, str(blocker))


def test_copy_detects_changed_size(tmp_path):
    item = make_item(tmp_path, "a.jpg", 5000)
    item.size = 6000
    with pytest.raises(copier.SourceError, match="changed"):
        copier.copy_file(item, str(tmp_path / "dst"))


def test_copy_cancel(tmp_path):
    item = make_item(tmp_path, "big.mov", 5 << 20)
    ev = threading.Event()
    ev.set()
    with pytest.raises(copier.Cancelled):
        copier.copy_file(item, str(tmp_path / "dst"), cancel=ev, chunk=1 << 20)
    assert not (tmp_path / "dst" / "big.mov").exists()


def test_copy_overwrites_previous_partial(tmp_path):
    item = make_item(tmp_path, "a.jpg", 4000)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / ("." + "a.jpg" + copier.TMP_SUFFIX)).write_bytes(b"half")
    (dst / "a.jpg").write_bytes(b"stale")
    copier.copy_file(item, str(dst))
    assert (dst / "a.jpg").read_bytes() == open(item.src, "rb").read()


# -- index --------------------------------------------------------------

def test_index_lookup_history_and_interrupt(tmp_path):
    idx = Index(str(tmp_path / "index" / "ingest.db"))
    a = idx.start_import(dest_folder="2026-09-26_1405_EOS_R6", verify_mode="hash",
                         label="EOS_R6", files_total=2, bytes_total=30)
    idx.add_file(a, "10:aaa", "DCIM/A.CR3", 10, 1.0, "h1")
    idx.add_file(a, "20:bbb", "DCIM/B.CR3", 20, 2.0, "h2")
    idx.finish_import(a, "done")
    b = idx.start_import(dest_folder="2026-09-27_0900_EOS_R6", verify_mode="none")
    idx.add_file(b, "10:aaa", "DCIM/A.CR3", 10, 1.0, None)
    assert idx.mark_interrupted() == 1
    assert idx.get_import(b)["status"] == "interrupted"
    known = idx.lookup(["10:aaa", "20:bbb", "30:ccc"])
    assert set(known) == {"10:aaa", "20:bbb"}
    assert known["10:aaa"].dest_folder == "2026-09-26_1405_EOS_R6"  # earliest import
    assert idx.known_sizes() == {10, 20}
    assert idx.get_import(a)["files_done"] == 2 and idx.get_import(a)["bytes_done"] == 30
    # search by folder name (not by date: the imports are started "now")
    assert [h["id"] for h in idx.history(query="_0900_")] == [b]
    assert idx.history(query="nothing-like-this") == []
    assert len(idx.history()) == 2
    idx.mark_verified(a, ["DCIM/A.CR3"])
    assert {f["status"] for f in idx.files_of(a)} == {"verified", "copied"}
    idx.backup_to(str(tmp_path / "backup.db"))
    assert Index(str(tmp_path / "backup.db")).known_sizes() == {10, 20}
    idx.close()
