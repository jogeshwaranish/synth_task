"""Tests for load_or_refresh_token — the refresh + rotation decision logic.

Network is faked at the httpx boundary (no real Strava calls, per CLAUDE.md).
The on-disk cache is real: rotation must persist the NEW refresh token, because
Strava invalidates the old one on every refresh.
"""

import time

import httpx

from config import Settings
from ingest import strava
from ingest.strava import TokenBundle, load_or_refresh_token, load_token, save_token


def _settings(tmp_path) -> Settings:
    # _env_file=None keeps the test hermetic — never reads the real .env.
    return Settings(
        _env_file=None,
        synth_token_dir=tmp_path,
        strava_client_id="cid",
        strava_client_secret="csecret",
    )


def _bundle(expires_in: int, refresh_token: str = "old_refresh") -> TokenBundle:
    return TokenBundle(
        access_token="old_access",
        refresh_token=refresh_token,
        expires_at=int(time.time()) + expires_in,
        scope="activity:read_all",
    )


def _fake_token_post(monkeypatch, calls: list[dict]) -> None:
    def post(url, data=None, timeout=None):
        calls.append({"url": url, "data": data})
        return httpx.Response(
            200,
            json={
                "access_token": "new_access",
                "refresh_token": "ROTATED_refresh",
                "expires_at": int(time.time()) + 21600,
                "athlete": {"id": 7},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(strava.httpx, "post", post)


def test_fresh_token_is_returned_without_any_network_call(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    tb = _bundle(expires_in=3600)
    save_token(tb, s.strava_token_path)
    calls: list[dict] = []
    _fake_token_post(monkeypatch, calls)

    assert load_or_refresh_token(s) == tb
    assert calls == []  # fresh token -> no refresh request


def test_expired_token_is_refreshed_and_rotation_persisted(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    save_token(_bundle(expires_in=10), s.strava_token_path)  # inside the 60s skew
    calls: list[dict] = []
    _fake_token_post(monkeypatch, calls)

    got = load_or_refresh_token(s)

    assert len(calls) == 1
    sent = calls[0]["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "old_refresh"
    assert sent["client_id"] == "cid" and sent["client_secret"] == "csecret"
    assert got.access_token == "new_access"
    assert got.refresh_token == "ROTATED_refresh"
    # The rotated refresh token MUST be what's on disk now — the old one is dead.
    assert load_token(s.strava_token_path).refresh_token == "ROTATED_refresh"


def test_force_refresh_refreshes_even_a_fresh_token(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    save_token(_bundle(expires_in=3600), s.strava_token_path)
    calls: list[dict] = []
    _fake_token_post(monkeypatch, calls)

    got = load_or_refresh_token(s, force_refresh=True)

    assert len(calls) == 1
    assert got.refresh_token == "ROTATED_refresh"


def test_missing_cache_falls_back_to_full_authorize(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    fresh = _bundle(expires_in=3600, refresh_token="from_authorize")
    monkeypatch.setattr(strava, "authorize", lambda _s: fresh)

    assert load_or_refresh_token(s) == fresh


def test_refresh_http_error_propagates_without_clobbering_cache(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    save_token(_bundle(expires_in=10), s.strava_token_path)

    def post(url, data=None, timeout=None):
        return httpx.Response(401, json={"message": "Authorization Error"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(strava.httpx, "post", post)

    try:
        load_or_refresh_token(s)
        raise AssertionError("a 401 refresh must raise")
    except httpx.HTTPStatusError:
        pass
    # The cached bundle is untouched — the user can re-authorize from it cleanly.
    assert load_token(s.strava_token_path).refresh_token == "old_refresh"
