"""Ed25519 identities. A girl's public key is her true name on Gossip."""

from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .config import atomic_write


class Identity:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key
        self.pub = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        if path.exists():
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))
        else:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, raw.hex() + "\n", mode=0o600)
        return cls(key)

    def sign(self, msg: bytes) -> str:
        return self._key.sign(msg).hex()


def verify(pub_hex: str, msg: bytes, sig_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def short(pub: str) -> str:
    return pub[:8]
