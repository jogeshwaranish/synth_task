"""Tests for fetch_activities — pagination over /athlete/activities.

httpx.Client is swapped for one backed by a MockTransport, so the paging loop
runs for real against canned pages (no network, per CLAUDE.md).
"""

import time

import httpx

from config import Settings
from ingest import strava
from ingest.strava import TokenBundle, fetch_activities


def _settings() -> Settings:
    return Settings(_env_file=None)


def _bundle() -> TokenBundle:
    return TokenBundle(
        access_token="ACCESS_123",
        refresh_token="ref",
        expires_at=int(time.time()) + 3600,
        scope="activity:read_all",
    )


# strava.httpx IS the httpx module, so the patched factory must hold a reference
# to the real Client class — calling httpx.Client inside it would recurse.
_RealClient = httpx.Client


def _install_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        strava.httpx, "Client", lambda **kw: _RealClient(transport=transport)
    )


def _install_pages(monkeypatch, pages: dict[int, list[dict]], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages.get(page, []))

    _install_transport(monkeypatch, handler)


def test_fetch_pages_until_an_empty_page(monkeypatch):
    pages = {
        1: [{"id": 1}, {"id": 2}],
        2: [{"id": 3}],
        3: [],  # terminator
    }
    seen: list[httpx.Request] = []
    _install_pages(monkeypatch, pages, seen)

    out = fetch_activities(_settings(), _bundle(), per_page=2)

    assert [a["id"] for a in out] == [1, 2, 3]
    assert [int(r.url.params["page"]) for r in seen] == [1, 2, 3]
    assert all(r.url.params["per_page"] == "2" for r in seen)


def test_fetch_sends_bearer_token_and_hits_activities_endpoint(monkeypatch):
    seen: list[httpx.Request] = []
    _install_pages(monkeypatch, {1: []}, seen)

    assert fetch_activities(_settings(), _bundle()) == []
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer ACCESS_123"
    assert seen[0].url.path == "/api/v3/athlete/activities"


def test_fetch_http_error_surfaces(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Rate Limit Exceeded"})

    _install_transport(monkeypatch, handler)

    try:
        fetch_activities(_settings(), _bundle())
        raise AssertionError("a 429 must raise, not return partial data")
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 429
