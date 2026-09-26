import base64
import gzip
import hashlib
import io
import tarfile

import pytest

from buildlib import apk


def P(name, deps="", provides="", install_if="", prio=0):
    return apk.Package(name=name, version="1.0-r0", repo="main", depends=deps.split(),
                       provides=provides.split(), install_if=install_if.split(),
                       provider_priority=prio)


def test_resolver_virtuals_and_priority():
    r = apk.Resolver([
        P("app", "so:libfoo.so.1 /bin/sh !conflict"),
        P("libfoo", provides="so:libfoo.so.1=1"),
        P("busybox-binsh", provides="/bin/sh", prio=100),
        P("bash-binsh", provides="/bin/sh", prio=1),
    ])
    assert set(r.resolve(["app"])) == {"app", "libfoo", "busybox-binsh"}


def test_resolver_install_if():
    r = apk.Resolver([
        P("openrc"), P("avahi"), P("avahi-openrc", install_if="avahi=1.0-r0 openrc"),
        P("avahi-doc", install_if="avahi docs"),
    ])
    sel = r.resolve(["avahi", "openrc"])
    assert "avahi-openrc" in sel and "avahi-doc" not in sel


def test_resolver_missing():
    with pytest.raises(KeyError, match="so:libmissing"):
        apk.Resolver([P("app", "so:libmissing.so")]).resolve(["app"])


def _gz_tar(files, eof=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    if not eof:  # apk control/signature segments have no end-of-archive blocks
        raw = raw.rstrip(b"\0")
        raw += b"\0" * ((512 - len(raw) % 512) % 512)
    return gzip.compress(raw, mtime=0)


def make_apk():
    data = _gz_tar({"usr/bin/hello": b"hi"}, eof=True)
    pkginfo = f"pkgname = hello\ndatahash = {hashlib.sha256(data).hexdigest()}\n".encode()
    control = _gz_tar({".PKGINFO": pkginfo})
    sig = _gz_tar({".SIGN.RSA.test.rsa.pub": b"sig"})
    pkg = P("hello")
    pkg.checksum = "Q1" + base64.b64encode(hashlib.sha1(control).digest()).decode()
    return sig + control + data, pkg


def test_verify_apk_ok_and_tampered():
    raw, pkg = make_apk()
    apk.verify_apk(raw, pkg)
    assert apk.apk_data_members(raw).extractfile("usr/bin/hello").read() == b"hi"
    other = P("hello")
    other.checksum = "Q1" + base64.b64encode(b"x" * 20).decode()
    with pytest.raises(apk.VerifyError, match="control checksum"):
        apk.verify_apk(raw, other)
    streams = apk.split_gzip_streams(raw)
    forged = streams[0][0] + streams[1][0] + _gz_tar({"usr/bin/hello": b"evil"}, eof=True)
    with pytest.raises(apk.VerifyError, match="data hash"):
        apk.verify_apk(forged, pkg)
