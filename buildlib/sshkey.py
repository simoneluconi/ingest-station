"""SSH key of the build machine, used to reach the station (tests and
`build.py --deploy`). The private key stays in keys/private/ (git-ignored);
its public half is installed as root's authorized key in the image."""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def ensure_key(path: str, comment: str = "ingest-station-build") -> str:
    """Create the Ed25519 key pair at `path` (+ .pub) if missing; return the
    public key line."""
    pub_path = path + ".pub"
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                            serialization.PublicFormat.OpenSSH)
        with open(path, "wb") as f:
            f.write(pem)
        with open(pub_path, "wb") as f:
            f.write(pub + f" {comment}\n".encode())
    with open(pub_path, encoding="ascii") as f:
        return f.read().strip()


def authorized_keys(build_pub: str, extra_path: str) -> bytes:
    """authorized_keys content: the build key plus the optional
    keys/authorized_keys file (e.g. the user's own keys)."""
    lines = [build_pub]
    if os.path.exists(extra_path):
        with open(extra_path, encoding="utf-8") as f:
            lines += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return ("\n".join(lines) + "\n").encode()
