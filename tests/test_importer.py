import os
import threading

import pytest

from ingest.daemon import copier, importer, scanner
from ingest.daemon.index import Index


def make_card(root, files):
    for rel, size in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))


def job(tmp_path, items, verify="hash", decide=None, index=None, source=None):
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    reports = []
    j = importer.ImportJob(
        items=items, verify=verify, share_root=str(share),
        index=index or Index(str(tmp_path / "idx.db")),
        source=source or {"label": "EOS_R6", "model": "SD", "name": "card"},
        schema="{date}_{time}_{label}", report=reports.append,
        decide=decide or (lambda err: "cancel"), ensure_share=lambda: None,
        cancel=threading.Event())
    return j, reports, share


CARD = {"DCIM/100CANON/IMG_0001.CR3": 3 << 20, "DCIM/100CANON/IMG_0002.CR3": 5000,
        "PRIVATE/M4ROOT/CLIP/C0001.MP4": 9 << 20}


def test_full_import_then_everything_is_known(tmp_path):
    make_card(tmp_path / "card", CARD)
    idx = Index(str(tmp_path / "idx.db"))
    scan = scanner.scan(str(tmp_path / "card"), idx)
    assert scan.new_files == 3 and scan.known_files == 0
    j, reports, share = job(tmp_path, scan.plan("new"), verify="full", index=idx)
    res = j.run()
    assert res.status == "done" and res.files == 3
    folder = share / res.folder
    assert res.folder.endswith("_EOS_R6")
    for rel in CARD:
        assert (folder / rel).read_bytes() == (tmp_path / "card" / rel).read_bytes()
    manifest = (folder / f"{res.folder}.xxh128").read_text().splitlines()
    assert len(manifest) == 3 and all("  DCIM/" in l or "  PRIVATE/" in l for l in manifest)
    assert (folder / f"{res.folder}.log").exists()
    assert (share / ".ingest-station" / "index.db").exists()
    assert {f["status"] for f in idx.files_of(j.import_id)} == {"verified"}
    assert reports[-1]["phase"] == "finishing"
    # a re-scan (e.g. after formatting in another reader: same files) knows them all
    again = scanner.scan(str(tmp_path / "card"), idx)
    assert again.known_files == 3 and again.new_files == 0
    assert again.previous[0]["folder"] == res.folder
    assert again.plan("new") == [] and len(again.plan("all")) == 3


def test_share_error_pauses_and_retry_resumes(tmp_path, monkeypatch):
    make_card(tmp_path / "card", CARD)
    idx = Index(str(tmp_path / "idx.db"))
    items = scanner.scan(str(tmp_path / "card"), idx).plan("new")
    real = copier.copy_file
    calls = {"n": 0}

    def flaky(item, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:   # the share drops on the second file
            raise copier.DestError("write error: Input/output error")
        return real(item, *a, **kw)

    monkeypatch.setattr(copier, "copy_file", flaky)
    decisions = []
    j, reports, share = job(tmp_path, items, index=idx,
                            decide=lambda err: decisions.append(err) or "retry")
    res = j.run()
    assert res.status == "done" and res.files == 3
    assert decisions == ["write error: Input/output error"]
    assert any(r.get("paused") for r in reports)
    assert len(idx.files_of(j.import_id)) == 3


def test_share_error_cancel_keeps_completed_files(tmp_path, monkeypatch):
    make_card(tmp_path / "card", CARD)
    idx = Index(str(tmp_path / "idx.db"))
    items = scanner.scan(str(tmp_path / "card"), idx).plan("new")
    real = copier.copy_file
    calls = {"n": 0}

    def flaky(item, *a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise copier.DestError("share offline")
        return real(item, *a, **kw)

    monkeypatch.setattr(copier, "copy_file", flaky)
    j, _, _ = job(tmp_path, items, index=idx, decide=lambda err: "cancel")
    res = j.run()
    assert res.status == "cancelled" and res.files == 1
    # re-insertion: only the missing files are new
    rescan = scanner.scan(str(tmp_path / "card"), idx)
    assert rescan.known_files == 1 and rescan.new_files == 2


def test_full_verify_detects_a_corrupted_copy(tmp_path, monkeypatch):
    make_card(tmp_path / "card", CARD)
    idx = Index(str(tmp_path / "idx.db"))
    items = scanner.scan(str(tmp_path / "card"), idx).plan("new")
    real_hash = copier.hash_file
    state = {"corrupted": False}

    def corrupt_once(path, **kw):
        if not state["corrupted"] and path.endswith("IMG_0002.CR3"):
            state["corrupted"] = True
            with open(path, "r+b") as f:
                f.write(b"X")
        return real_hash(path, **kw)

    monkeypatch.setattr(copier, "hash_file", corrupt_once)
    errors = []
    j, _, share = job(tmp_path, items, verify="full", index=idx,
                      decide=lambda err: errors.append(err) or "retry")
    res = j.run()
    assert res.status == "done"
    assert len(errors) == 1 and "1 files differ" in errors[0]
    rel = "DCIM/100CANON/IMG_0002.CR3"
    assert (share / res.folder / rel).read_bytes() == (tmp_path / "card" / rel).read_bytes()


def test_source_error_fails_the_import(tmp_path):
    make_card(tmp_path / "card", CARD)
    idx = Index(str(tmp_path / "idx.db"))
    items = scanner.scan(str(tmp_path / "card"), idx).plan("new")
    os.remove(items[1].src)   # card pulled out
    j, _, _ = job(tmp_path, items, index=idx)
    res = j.run()
    assert res.status == "failed" and res.error.startswith("source:")


def test_scanner_skips_system_files(tmp_path):
    make_card(tmp_path / "card", {"DCIM/A.JPG": 10, "System Volume Information/x": 5,
                                  ".Trashes/y": 5, "DCIM/._A.JPG": 3, "DCIM/.DS_Store": 3})
    items = scanner.walk(str(tmp_path / "card"))
    assert [i.rel for i in items] == ["DCIM/A.JPG"]
