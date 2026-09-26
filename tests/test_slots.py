import json
import os
import re

import pytest

from ingest.common import grubenv, slots

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_esp(tmp_path, env=None, slot_names=("A",)):
    esp = tmp_path / "esp"
    (esp / "boot" / "grub").mkdir(parents=True)
    grubenv.create(str(esp / "boot" / "grub" / "grubenv"), env or {"active_slot": "A"})
    for s in slot_names:
        d = esp / "slots" / s
        d.mkdir(parents=True)
        (d / "vmlinuz-lts").write_bytes(b"kernel " + s.encode())
        files = {"vmlinuz-lts": slots.sha256_file(str(d / "vmlinuz-lts"))}
        (d / "slot.json").write_text(json.dumps({"version": "0.1.0", "files": files}))
    return esp


def test_trial_then_confirm(tmp_path):
    esp = make_esp(tmp_path, slot_names=("A", "B"))
    slots.SlotManager(str(esp), "A", remount=False).start_trial("B", 3)
    env = grubenv.read(str(esp / "boot/grub/grubenv"))
    assert env["try_slot"] == "B" and env["try_left"] == "3"

    mgr_b = slots.SlotManager(str(esp), "B", remount=False)
    assert mgr_b.on_trial()
    assert mgr_b.mark_good() is True
    env = grubenv.read(str(esp / "boot/grub/grubenv"))
    assert env["active_slot"] == "B"
    assert "try_slot" not in env and "try_left" not in env
    assert mgr_b.mark_good() is False  # already confirmed


def test_fallback_boot_does_not_confirm(tmp_path):
    # GRUB rolled back to A after B failed: A must not claim B's trial
    esp = make_esp(tmp_path, {"active_slot": "A", "try_failed": "B"}, ("A", "B"))
    mgr = slots.SlotManager(str(esp), "A", remount=False)
    assert mgr.mark_good() is False
    assert mgr.status()["try_failed"] == "B"
    mgr.ack_failure()
    assert mgr.status()["try_failed"] == ""


def test_trial_requires_system_in_slot(tmp_path):
    esp = make_esp(tmp_path)
    mgr = slots.SlotManager(str(esp), "A", remount=False)
    with pytest.raises(slots.SlotError):
        mgr.start_trial("B")
    with pytest.raises(slots.SlotError):
        mgr.start_trial("A", tries=5)


def test_set_active_verifies_files(tmp_path):
    esp = make_esp(tmp_path, slot_names=("A", "B"))
    mgr = slots.SlotManager(str(esp), "A", remount=False)
    (esp / "slots" / "B" / "vmlinuz-lts").write_bytes(b"corrupted")
    with pytest.raises(slots.SlotError, match="corrupt vmlinuz-lts"):
        mgr.set_active("B")
    (esp / "slots" / "B" / "vmlinuz-lts").write_bytes(b"kernel B")
    mgr.set_active("B")
    assert mgr.status()["active"] == "B"


def test_clone_current(tmp_path):
    esp = make_esp(tmp_path)
    mgr = slots.SlotManager(str(esp), "A", remount=False)
    mgr.clone_current(log=lambda m: None)
    assert slots.verify_slot(str(esp / "slots" / "B")) == []
    assert mgr.status()["slots"]["B"]["present"]


def test_find_esp(tmp_path):
    esp = make_esp(tmp_path)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/sda1 /media/sda1 vfat ro 0 0\n/dev/mmcblk0p1 {esp} vfat ro 0 0\n")
    assert slots.find_esp(str(mounts)) == str(esp)


def test_grub_countdown_covers_allowed_tries():
    path = os.path.join(ROOT, "buildlib", "boot", "grub-system.cfg")
    cfg = open(path, encoding="utf-8").read()
    for n in (1, 2, 3):
        assert re.search(rf'\[ "\$try_left" = "{n}" \]; then\s+set try_left={n - 1}', cfg)
    assert "save_env try_slot try_left try_failed" in cfg
