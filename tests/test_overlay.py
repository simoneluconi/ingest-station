import io
import tarfile

import pytest

from buildlib.overlay import Overlay, OverlayError


def test_overlay_ownership_modes_links(tmp_path):
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    (tmp_path / "etc" / "init.d" / "svc").write_bytes(b"#!/sbin/openrc-run\n")
    (tmp_path / "etc" / "hostname").write_bytes(b"ingest\n")
    ov = Overlay(mtime=1700000000)
    ov.add_tree(str(tmp_path))
    ov.symlink("etc/runlevels/default/svc", "/etc/init.d/svc")
    ov.file("data/creds.secret", b"x", 0o600)
    t = tarfile.open(fileobj=io.BytesIO(ov.tar_gz()))
    m = {x.name: x for x in t.getmembers()}
    assert all(x.uid == 0 and x.gid == 0 and x.uname == "root" for x in m.values())
    assert m["etc/init.d/svc"].mode == 0o755
    assert m["etc/hostname"].mode == 0o644
    assert m["data/creds.secret"].mode == 0o600
    assert m["etc/runlevels/default/svc"].issym()
    assert m["etc/runlevels/default/svc"].linkname == "/etc/init.d/svc"
    assert m["etc/runlevels"].isdir()


def test_overlay_rejects_crlf(tmp_path):
    (tmp_path / "script.sh").write_bytes(b"echo hi\r\n")
    with pytest.raises(OverlayError, match="CRLF"):
        Overlay(mtime=0).add_tree(str(tmp_path))
