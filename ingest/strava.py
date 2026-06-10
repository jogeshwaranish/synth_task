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
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from schemas import Activity, Source, Sport

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
