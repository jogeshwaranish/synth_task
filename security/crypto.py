"""Local symmetric encryption for secrets at rest. Owners: Anish (security).

Plugs into the `# TODO(security)` seams — currently the Strava token cache
(ingest/strava.py); the DB-field seam in store/db.py reuses the same primitives.

Cipher: AES-256-GCM (authenticated encryption). A random 12-byte nonce is
generated per call and prepended to the ciphertext; GCM's tag means any
tampering (or a wrong key) fails loudly on decrypt with `InvalidTag` rather
than returning garbage.

Key management (see DECISIONS.md): the 32-byte key is AUTO-GENERATED on first
use and stored in a keyfile (0600, inside the gitignored .tokens/ dir). It is
never committed and never transported between machines — each collaborator's
machine generates its own, because the data it protects (the per-account token
cache) is itself per-machine. There is no shared secret to manage.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_BYTES = 32   # AES-256
_NONCE_BYTES = 12  # GCM standard nonce length


def load_or_create_key(path: str | Path) -> bytes:
    """Return the 32-byte key at `path`, generating + persisting it if absent.

    The keyfile is written 0600 (owner-only). Existing files are validated to be
    exactly 32 bytes so a truncated/corrupt key fails here, not mid-decrypt.
    """
    path = Path(path)
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise ValueError(
                f"keyfile {path} is {len(key)} bytes, expected {_KEY_BYTES}"
            )
        return key
    key = os.urandom(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic 0600 create — the key must never be group/other-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) || ciphertext||tag."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Inverse of `encrypt`. Raises `InvalidTag` on tamper or wrong key."""
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct, None)
