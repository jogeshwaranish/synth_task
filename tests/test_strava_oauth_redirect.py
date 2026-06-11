"""Tests for the one-shot localhost OAuth redirect catcher.

These spin up the real HTTP server on an ephemeral port and hit it with a real
loopback request — that's localhost only, not the network fixture rule's target.
Covers the denial surfacing and stale-code reset from the OAuth hardening fix.
"""

import socket
import threading
import time

import httpx

from ingest.strava import _catch_redirect_code, _CodeCatcher


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _run_catcher(port: int) -> dict:
    """Start _catch_redirect_code in a thread; returns a result/error slot."""
    slot: dict = {}

    def run():
        try:
            slot["code"] = _catch_redirect_code(port)
        except Exception as e:  # surfaced to the test, not swallowed
            slot["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    slot["thread"] = t
    return slot


def _get_with_retry(path_and_query: str, port: int) -> httpx.Response:
    # The server binds inside the catcher thread; retry briefly until it's up.
    # 127.0.0.1 explicitly — "localhost" can resolve to ::1 first and time out
    # against the IPv4-only HTTPServer.
    url = f"http://127.0.0.1:{port}{path_and_query}"
    for _ in range(50):
        try:
            return httpx.get(url, timeout=1)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            time.sleep(0.02)
    raise AssertionError("redirect catcher never came up")


def test_catcher_returns_the_authorization_code(tmp_path):
    port = _free_port()
    slot = _run_catcher(port)
    resp = _get_with_retry("/callback?code=abc123&scope=read", port)
    slot["thread"].join(timeout=5)

    assert resp.status_code == 200
    assert slot.get("code") == "abc123"
    # The code itself must not be echoed back into the browser tab.
    assert "abc123" not in resp.text


def test_catcher_surfaces_a_denial_explicitly():
    port = _free_port()
    slot = _run_catcher(port)
    _get_with_retry("/callback?error=access_denied", port)
    slot["thread"].join(timeout=5)

    assert "code" not in slot
    assert "denied" in str(slot["error"])
    assert "access_denied" in str(slot["error"])


def test_catcher_rejects_a_redirect_with_no_code():
    port = _free_port()
    slot = _run_catcher(port)
    _get_with_retry("/callback", port)
    slot["thread"].join(timeout=5)

    assert "code" not in slot
    assert "Did not receive" in str(slot["error"])


def test_stale_code_from_a_previous_run_is_never_reused():
    # Regression for the OAuth hardening fix: class-level state must be reset,
    # so a denial after a previously successful run raises instead of returning
    # the old code.
    _CodeCatcher.code = "STALE_FROM_LAST_RUN"
    _CodeCatcher.error = None
    port = _free_port()
    slot = _run_catcher(port)
    _get_with_retry("/callback?error=access_denied", port)
    slot["thread"].join(timeout=5)

    assert slot.get("code") != "STALE_FROM_LAST_RUN"
    assert "denied" in str(slot["error"])
