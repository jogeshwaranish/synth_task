"""Strava ingestion: OAuth (later task), fetch (later task), and the pure normalizer.

Strava quirk: start_date_local is the athlete's WALL-CLOCK time with a 'Z'
suffix — it is NOT UTC. We parse it as naive local and take local_date from it,
per the contract's join rule. start_date is the real UTC instant.

UntrustedText fields (name, device_name) originate at Strava — never interpolate
into a prompt without delimiter wrapping, nor into SQL without parameterization.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from config import Settings
from schemas import Activity, Source, Sport
from store import db

_M_PER_MILE = 1609.344
_FT_PER_M = 3.280839895013123
_MPH_PER_MPS = 2.2369362920544025


def _parse_local(s: str) -> datetime:
    # "2026-05-13T09:40:31Z" -> naive local wall-clock (drop the false Z).
    return datetime.fromisoformat(s.replace("Z", ""))


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_activity(raw: dict, *, athlete_id: str) -> Activity:
    start_local = _parse_local(raw["start_date_local"])
    sport_raw = raw.get("sport_type") or raw.get("type") or "Other"
    dist_m = float(raw.get("distance") or 0.0)
    elev_m = raw.get("total_elevation_gain")
    speed_mps = raw.get("average_speed")
    return Activity(
        activity_id=str(raw["id"]),
        source=Source.STRAVA_API,
        athlete_id=athlete_id,
        start_local=start_local,
        start_utc=_parse_utc(raw.get("start_date")),
        local_date=start_local.date(),
        name=raw.get("name") or "",
        sport=Sport.normalize(sport_raw),
        is_trainer=bool(raw.get("trainer", False)),
        moving_time_sec=float(raw.get("moving_time") or 0),
        elapsed_time_sec=_opt_float(raw.get("elapsed_time")),
        distance_mi=dist_m / _M_PER_MILE,
        elevation_gain_ft=None if elev_m is None else float(elev_m) * _FT_PER_M,
        avg_speed_mph=None if speed_mps is None else float(speed_mps) * _MPH_PER_MPS,
        avg_hr=_opt_float(raw.get("average_heartrate")),
        max_hr=_opt_float(raw.get("max_heartrate")),
        avg_watts=_opt_float(raw.get("average_watts")),
        weighted_watts=_opt_float(raw.get("weighted_average_watts")),
        kilojoules=_opt_float(raw.get("kilojoules")),
        avg_cadence=_opt_float(raw.get("average_cadence")),
        suffer_score=_opt_float(raw.get("suffer_score")),
        calories=_opt_float(raw.get("calories")),       # detail endpoint only
        perceived_exertion=_opt_float(raw.get("perceived_exertion")),
        device_name=raw.get("device_name"),             # detail endpoint only
    )


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


_EXPIRY_SKEW_SEC = 60


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: int            # unix epoch seconds
    scope: str
    athlete_id: int | None = None


def _is_expired(tb: TokenBundle) -> bool:
    return tb.expires_at <= int(time.time()) + _EXPIRY_SKEW_SEC


def save_token(tb: TokenBundle, path: str | Path) -> None:
    # TODO(security): Anish — this refresh token is a long-lived secret; the
    # at-rest encryption hook wraps this write. For now: 0600, outside git.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(tb), f)


def load_token(path: str | Path) -> TokenBundle | None:
    path = Path(path)
    if not path.exists():
        return None
    with path.open() as f:
        return TokenBundle(**json.load(f))


_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
_TOKEN_URL = "https://www.strava.com/oauth/token"
_API = "https://www.strava.com/api/v3"


def _authorize_url(s: Settings) -> str:
    q = urlencode({
        "client_id": s.strava_client_id,
        "response_type": "code",
        "redirect_uri": s.redirect_uri,
        "approval_prompt": "auto",
        "scope": s.strava_scope,
    })
    return f"{_AUTHORIZE_URL}?{q}"


class _CodeCatcher(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        _CodeCatcher.code = (qs.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        ok = b"Strava authorized. You can close this tab and return to the CLI."
        self.wfile.write(ok if _CodeCatcher.code else b"No code returned.")

    def log_message(self, *_):  # silence access logging (avoid leaking the code)
        return


def _catch_redirect_code(port: int) -> str:
    server = HTTPServer(("localhost", port), _CodeCatcher)
    t = threading.Thread(target=server.handle_request)  # serve exactly one req
    t.start()
    t.join(timeout=300)
    server.server_close()
    if not _CodeCatcher.code:
        raise RuntimeError("Did not receive an authorization code from Strava.")
    return _CodeCatcher.code


def _token_request(s: Settings, payload: dict) -> TokenBundle:
    payload = {"client_id": s.strava_client_id,
               "client_secret": s.strava_client_secret, **payload}
    resp = httpx.post(_TOKEN_URL, data=payload, timeout=30)
    resp.raise_for_status()  # 4xx/5xx surface status, never the secret payload
    d = resp.json()
    athlete = d.get("athlete") or {}
    return TokenBundle(
        access_token=d["access_token"],
        refresh_token=d["refresh_token"],   # Strava ROTATES this — always persist
        expires_at=int(d["expires_at"]),
        scope=s.strava_scope,
        athlete_id=athlete.get("id"),
    )


def authorize(s: Settings) -> TokenBundle:
    if not s.strava_client_id or not s.strava_client_secret:
        raise RuntimeError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env")
    url = _authorize_url(s)
    print("Opening browser for Strava authorization...")
    webbrowser.open(url)
    print(f"If it didn't open, visit:\n  {url}")
    code = _catch_redirect_code(s.strava_redirect_port)
    tb = _token_request(s, {"code": code, "grant_type": "authorization_code"})
    save_token(tb, s.strava_token_path)
    return tb


def load_or_refresh_token(s: Settings, *, force_refresh: bool = False) -> TokenBundle:
    tb = load_token(s.strava_token_path)
    if tb is None:
        return authorize(s)
    if force_refresh or _is_expired(tb):
        tb = _token_request(
            s, {"grant_type": "refresh_token", "refresh_token": tb.refresh_token}
        )
        save_token(tb, s.strava_token_path)  # persist the rotated refresh token
    return tb


def fetch_activities(s: Settings, tb: TokenBundle, *, per_page: int = 200) -> list[dict]:
    headers = {"Authorization": f"Bearer {tb.access_token}"}
    out: list[dict] = []
    page = 1
    with httpx.Client(timeout=60) as client:
        while True:
            r = client.get(
                f"{_API}/athlete/activities",
                headers=headers,
                params={"per_page": per_page, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            page += 1
    return out


def sync_strava(s: Settings, conn, *, force_refresh: bool = False) -> int:
    tb = load_or_refresh_token(s, force_refresh=force_refresh)
    raw = fetch_activities(s, tb)
    activities = [to_activity(r, athlete_id=s.strava_athlete_id) for r in raw]
    return db.upsert_activities(conn, activities)
