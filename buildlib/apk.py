"""Alpine package (.apk v2) and APKINDEX handling: signature/hash
verification, parsing and a dependency resolver."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
import zlib
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class VerifyError(Exception):
    pass


def split_gzip_streams(raw: bytes) -> list[tuple[bytes, bytes]]:
    """Split concatenated gzip members into (compressed, decompressed) pairs."""
    out, pos = [], 0
    while pos < len(raw):
        d = zlib.decompressobj(31)
        plain = d.decompress(raw[pos:]) + d.flush()
        end = len(raw) - len(d.unused_data)
        out.append((raw[pos:end], plain))
        pos = end
    return out


def _tar_members(plain: bytes) -> dict[str, bytes]:
    # apk segments are tar streams without end-of-archive blocks
    members = {}
    with tarfile.open(fileobj=io.BytesIO(plain + b"\0" * 1024), mode="r:") as t:
        for m in t.getmembers():
            if m.isfile():
                members[m.name] = t.extractfile(m).read()
    return members


def verify_rsa_sha1(pubkey_pem: bytes, signature: bytes, data: bytes) -> bool:
    key = serialization.load_pem_public_key(pubkey_pem)
    try:
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:
        return False


def verify_signed_stream(raw: bytes, keys: dict[str, bytes]) -> list[tuple[bytes, bytes]]:
    """Verify the leading signature member of an APKINDEX or .apk against
    the trusted `keys` (file name -> PEM). Returns the gzip streams."""
    streams = split_gzip_streams(raw)
    if len(streams) < 2:
        raise VerifyError("missing signature segment")
    sig_members = _tar_members(streams[0][1])
    signed = streams[1][0]
    for name, sig in sig_members.items():
        m = re.fullmatch(r"\.SIGN\.RSA\.(.+)", name)
        if m and m.group(1) in keys:
            if verify_rsa_sha1(keys[m.group(1)], sig, signed):
                return streams
            raise VerifyError(f"bad signature from {m.group(1)}")
    raise VerifyError(f"no trusted signature (found {list(sig_members)})")


# --------------------------------------------------------------------------
# APKINDEX


@dataclass
class Package:
    name: str
    version: str
    repo: str
    checksum: str = ""
    size: int = 0
    installed_size: int = 0
    depends: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    install_if: list[str] = field(default_factory=list)
    provider_priority: int = 0

    @property
    def filename(self) -> str:
        return f"{self.name}-{self.version}.apk"


def parse_apkindex(text: str, repo: str) -> list[Package]:
    pkgs = []
    for block in text.split("\n\n"):
        d: dict[str, str] = {}
        for line in block.splitlines():
            if len(line) > 2 and line[1] == ":":
                d[line[0]] = line[2:]
        if "P" not in d:
            continue
        pkgs.append(Package(
            name=d["P"], version=d["V"], repo=repo, checksum=d.get("C", ""),
            size=int(d.get("S", 0)), installed_size=int(d.get("I", 0)),
            depends=d.get("D", "").split(), provides=d.get("p", "").split(),
            install_if=d.get("i", "").split(), provider_priority=int(d.get("k", 0) or 0),
        ))
    return pkgs


def load_apkindex(raw: bytes, repo: str, keys: dict[str, bytes]) -> list[Package]:
    streams = verify_signed_stream(raw, keys)
    members = _tar_members(streams[1][1])
    return parse_apkindex(members["APKINDEX"].decode("utf-8", errors="replace"), repo)


def verify_apk(raw: bytes, pkg: Package) -> None:
    """Check a downloaded .apk against its (signed) APKINDEX entry: the SHA1
    of the control segment must match C:, and the data segment must match
    the sha256 `datahash` recorded in the control segment."""
    streams = split_gzip_streams(raw)
    if len(streams) != 3:
        raise VerifyError(f"{pkg.filename}: expected 3 segments, got {len(streams)}")
    control_raw, control_plain = streams[1]
    if not pkg.checksum.startswith("Q1"):
        raise VerifyError(f"{pkg.filename}: unsupported checksum {pkg.checksum!r}")
    want = base64.b64decode(pkg.checksum[2:])
    if hashlib.sha1(control_raw).digest() != want:
        raise VerifyError(f"{pkg.filename}: control checksum mismatch")
    pkginfo = _tar_members(control_plain).get(".PKGINFO", b"").decode()
    m = re.search(r"^datahash = ([0-9a-f]{64})$", pkginfo, re.M)
    if not m:
        raise VerifyError(f"{pkg.filename}: no datahash in .PKGINFO")
    if hashlib.sha256(streams[2][0]).hexdigest() != m.group(1):
        raise VerifyError(f"{pkg.filename}: data hash mismatch")


def apk_data_members(raw: bytes) -> tarfile.TarFile:
    """Open the data segment of an .apk as a tarfile."""
    plain = split_gzip_streams(raw)[2][1]
    return tarfile.open(fileobj=io.BytesIO(plain), mode="r:")


# --------------------------------------------------------------------------
# Resolver

_DEP_RE = re.compile(r"^(!?)([^<>=~]+)(?:([<>=~]+)(.*))?$")


def _dep_name(token: str) -> tuple[bool, str]:
    m = _DEP_RE.match(token)
    if not m:
        raise ValueError(f"bad dependency token {token!r}")
    return m.group(1) == "!", m.group(2)


class Resolver:
    """Resolve a package closure the same way apk does for our purposes:
    real names first, then virtual providers (so:, cmd:, pc:, ...), picking
    the highest provider_priority; install_if packages are pulled in when
    their conditions are satisfied."""

    def __init__(self, packages: list[Package]):
        self.by_name: dict[str, Package] = {}
        self.providers: dict[str, list[Package]] = {}
        for p in packages:
            # keep main over community when both exist; within a repo the
            # index holds a single version
            self.by_name.setdefault(p.name, p)
        for p in self.by_name.values():
            for prov in p.provides:
                _, n = _dep_name(prov)
                self.providers.setdefault(n, []).append(p)

    def _pick(self, name: str, selected: dict[str, Package]) -> Package:
        if name in self.by_name:
            return self.by_name[name]
        cands = self.providers.get(name)
        if not cands:
            raise KeyError(f"nothing provides {name!r}")
        for c in cands:
            if c.name in selected:
                return c
        return sorted(cands, key=lambda c: (-c.provider_priority, c.name))[0]

    def resolve(self, world: list[str]) -> dict[str, Package]:
        selected: dict[str, Package] = {}
        missing: list[str] = []

        def add(names: list[str], why: str) -> None:
            stack = list(names)
            while stack:
                tok = stack.pop()
                neg, name = _dep_name(tok)
                if neg:
                    continue
                try:
                    p = self._pick(name, selected)
                except KeyError:
                    missing.append(f"{name} (needed by {why})")
                    continue
                if p.name in selected:
                    continue
                selected[p.name] = p
                stack.extend(p.depends)

        add(world, "world")
        changed = True
        while changed:
            changed = False
            for p in self.by_name.values():
                if p.install_if and p.name not in selected:
                    if all(_dep_name(c)[1] in selected for c in p.install_if):
                        add([p.name], "install_if")
                        changed = True
        if missing:
            raise KeyError("unresolvable dependencies: " + ", ".join(missing))
        return selected
