import time

from ingest.strava import TokenBundle, load_token, save_token, _is_expired


def test_token_roundtrips_to_disk_with_locked_permissions(tmp_path):
    p = tmp_path / "tok.json"
    tb = TokenBundle(
        access_token="acc", refresh_token="ref",
        expires_at=int(time.time()) + 3600, scope="activity:read_all",
    )
    save_token(tb, p)
    assert load_token(p) == tb
    # 0600 — owner-only, no group/other read of the refresh token.
    assert (p.stat().st_mode & 0o777) == 0o600


def test_token_is_encrypted_at_rest(tmp_path):
    p = tmp_path / "tok.json"
    tb = TokenBundle(
        access_token="acc", refresh_token="SUPER_SECRET_REFRESH",
        expires_at=int(time.time()) + 3600, scope="activity:read_all",
    )
    save_token(tb, p)
    raw = p.read_bytes()
    # The refresh token must NOT sit in cleartext on disk.
    assert b"SUPER_SECRET_REFRESH" not in raw
    assert b"access_token" not in raw          # not even the JSON keys leak
    # ...but it round-trips back through the auto-created keyfile.
    assert load_token(p) == tb
    assert (tmp_path / "synth.key").exists()


def test_legacy_plaintext_token_is_migrated_on_load(tmp_path):
    import json
    from dataclasses import asdict

    p = tmp_path / "tok.json"
    tb = TokenBundle(
        access_token="acc", refresh_token="LEGACY_SECRET",
        expires_at=int(time.time()) + 3600, scope="activity:read_all",
        athlete_id=99,
    )
    # Simulate the OLD (pre-encryption) code path: plaintext JSON, no keyfile.
    p.write_text(json.dumps(asdict(tb)))
    assert b"LEGACY_SECRET" in p.read_bytes()     # precondition: plaintext on disk
    # Loading returns the bundle AND transparently re-encrypts in place.
    assert load_token(p) == tb
    assert b"LEGACY_SECRET" not in p.read_bytes()  # migrated to ciphertext
    assert (tmp_path / "synth.key").exists()
    assert load_token(p) == tb                     # still readable after migration


def test_expiry_uses_a_safety_skew():
    soon = TokenBundle("a", "r", int(time.time()) + 30, "s")
    fresh = TokenBundle("a", "r", int(time.time()) + 3600, "s")
    assert _is_expired(soon) is True   # within the 60s skew
    assert _is_expired(fresh) is False


def test_load_missing_token_returns_none(tmp_path):
    assert load_token(tmp_path / "nope.json") is None
