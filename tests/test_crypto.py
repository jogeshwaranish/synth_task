"""Tests for security/crypto.py — AES-256-GCM at-rest encryption.

The key is auto-generated per machine and never transported (see DECISIONS.md).
These tests exercise the pure primitives: key creation, round-trip, nonce
randomization, and tamper detection.
"""

from cryptography.exceptions import InvalidTag

from security import crypto


def test_key_autocreated_with_locked_perms(tmp_path):
    p = tmp_path / "synth.key"
    assert not p.exists()
    key = crypto.load_or_create_key(p)
    assert len(key) == 32                       # 256-bit
    assert p.exists()
    assert (p.stat().st_mode & 0o777) == 0o600  # owner-only
    # Second call returns the SAME key — it is not regenerated.
    assert crypto.load_or_create_key(p) == key


def test_encrypt_decrypt_roundtrips(tmp_path):
    key = crypto.load_or_create_key(tmp_path / "synth.key")
    plaintext = b'{"refresh_token": "super-secret"}'
    blob = crypto.encrypt(plaintext, key)
    assert blob != plaintext                    # actually encrypted
    assert plaintext not in blob                # secret not sitting in the blob
    assert crypto.decrypt(blob, key) == plaintext


def test_each_encryption_uses_a_fresh_nonce(tmp_path):
    key = crypto.load_or_create_key(tmp_path / "synth.key")
    msg = b"same message"
    # Random 12-byte nonce per call -> two ciphertexts differ even for one input.
    assert crypto.encrypt(msg, key) != crypto.encrypt(msg, key)


def test_decrypt_detects_tampering(tmp_path):
    key = crypto.load_or_create_key(tmp_path / "synth.key")
    blob = bytearray(crypto.encrypt(b"trust me", key))
    blob[-1] ^= 0x01                            # flip a bit in the GCM tag
    try:
        crypto.decrypt(bytes(blob), key)
        raise AssertionError("tampered ciphertext must not decrypt")
    except InvalidTag:
        pass


def test_wrong_key_cannot_decrypt(tmp_path):
    key_a = crypto.load_or_create_key(tmp_path / "a.key")
    key_b = crypto.load_or_create_key(tmp_path / "b.key")
    blob = crypto.encrypt(b"secret", key_a)
    try:
        crypto.decrypt(blob, key_b)
        raise AssertionError("decrypt with the wrong key must fail")
    except InvalidTag:
        pass
