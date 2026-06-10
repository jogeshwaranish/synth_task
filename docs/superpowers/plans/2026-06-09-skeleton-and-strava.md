# Skeleton + Strava Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the repo skeleton and a working Strava ingestion path that OAuths against a real account, normalizes activities into the `Activity` contract model, and persists them to SQLite — ending at a manual test checkpoint.

**Architecture:** Flat top-level packages at the repo root (honors CONTRACT.md's bare module paths). `config.py` loads secrets from `.env`. `store/db.py` owns all SQLite with `?`-parameterized SQL. `ingest/strava.py` runs a local-redirect OAuth flow, caches+rotates the refresh token in `.tokens/` (gitignored), fetches activities, and normalizes them with `to_activity()`. `cli.py sync` wires it together. The live OAuth + fetch is verified manually; everything pure (normalizer, token-expiry, db round-trip) is unit-tested.

**Tech Stack:** Python 3.12, uv, pydantic v2 / pydantic-settings, httpx, stdlib sqlite3, stdlib http.server + webbrowser (OAuth redirect catch), pytest.

**Scope boundary:** This plan stops at the Strava checkpoint. Sheet ingestion, the daily join, metrics/anomalies, and the synthesis agent are a separate follow-on plan written after this slice is tested.

**Convention for all commits in this plan:** end the message body with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` and commit as
`Basil Liu <basilpl2@illinois.edu>`. Never `git add` `.env`, `.tokens/`, `*.db`,
or the `*.csv` / `*.xlsx` data files (already gitignored).

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `pyproject.toml` | deps + uv config + flat-package discovery | exists |
| `.gitignore` | secrets, token cache, db, real-data fixtures | exists |
| `.env.example` | documented env template | exists |
| `config.py` | typed settings + secret redaction | exists |
| `insight_schema.json` | generated from `schemas.py` | Task 1 |
| `store/db.py` | sqlite3 connect/init + activity upsert/read | Task 2 |
| `ingest/strava.py` | OAuth + token cache/rotate + fetch + normalize | Tasks 4–6 |
| `cli.py` | `sync` entry point | Task 7 |
| `CLAUDE.md`, `DECISIONS.md` | repo conventions + decision log | Task 8 |
| `tests/` | normalizer, db round-trip, token-expiry tests | Tasks 2,3,4 |

---

## Task 1: Bootstrap env + generate insight_schema.json

**Files:**
- Modify: `pyproject.toml` (exists — no change expected, just lock)
- Create: `insight_schema.json` (generated)
- Create: `README.md` (referenced by pyproject `readme`)

- [ ] **Step 1: Create the venv and install deps**

Run:
```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```
Expected: resolves and installs pydantic, httpx, fastapi, openpyxl, anthropic, jsonschema, pytest.

- [ ] **Step 2: Add a minimal README (pyproject references it)**

Create `README.md`:
```markdown
# synth-task

Local backend for the synth MVP: pulls Strava + the founder's Google Sheet,
normalizes into the v1.0 contract (`schemas.py`), stores in SQLite at two grains,
computes training-load metrics + anomalies, and runs an Anthropic agent that
emits a `SynthesisReport`.

## Setup
    uv venv --python 3.12 && uv pip install -e ".[dev]"
    cp .env.example .env   # fill in STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET

## Usage
    uv run synth sync       # pull Strava (+ sheet, later) into synth.db
    uv run synth analyze    # compute metrics + anomalies (later)
    uv run synth report     # run the synthesis agent (later)

See `docs/superpowers/specs/` for the design and `DECISIONS.md` for tradeoffs.
```

- [ ] **Step 3: Generate the JSON Schema the validator enforces**

Run:
```bash
uv run python schemas.py
```
Expected: prints `contract v1.0 -> insight_schema.json` and writes `insight_schema.json`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml README.md insight_schema.json
git commit -m "chore: bootstrap uv env, README, generated insight_schema.json"
```

---

## Task 2: store/db.py — activity grain (schema + upsert + read)

**Files:**
- Create: `store/__init__.py` (empty)
- Create: `store/db.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db.py`:
```python
from datetime import date, datetime

from schemas import Activity, Source, Sport
from store import db


def _sample_activity(**over) -> Activity:
    base = dict(
        activity_id="A0004",
        source=Source.STRAVA_API,
        athlete_id="basil",
        start_local=datetime(2026, 5, 13, 9, 40, 31),
        start_utc=datetime(2026, 5, 13, 23, 40, 31),
        local_date=date(2026, 5, 13),
        name="Afternoon Run",
        sport=Sport.RUN,
        is_trainer=False,
        moving_time_sec=3501.0,
        distance_mi=6.178664676,
        elevation_gain_ft=921.91604,
        avg_speed_mph=6.3529096,
        avg_hr=147.9,
        max_hr=169.0,
    )
    base.update(over)
    return Activity(**base)


def test_upsert_then_read_roundtrips(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    a = _sample_activity()
    n = db.upsert_activities(conn, [a])
    assert n == 1
    got = db.get_activities(conn, athlete_id="basil")
    assert got == [a]


def test_upsert_is_idempotent_on_activity_id(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    db.upsert_activities(conn, [_sample_activity(name="old")])
    db.upsert_activities(conn, [_sample_activity(name="new")])
    got = db.get_activities(conn)
    assert len(got) == 1
    assert got[0].name == "new"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'store'`.

- [ ] **Step 3: Write store/db.py**

Create `store/__init__.py` (empty) and `tests/__init__.py` (empty).

Create `store/db.py`:
```python
"""SQLite persistence. All values are bound via ? — never string-formatted.

TODO(security): Anish — encryption-at-rest wrapper plugs in around connect().
Owners: Basil (schema + queries).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from schemas import Activity

# Field order is the single source of truth for both the table and the binds.
ACTIVITY_COLUMNS: tuple[str, ...] = (
    "activity_id", "source", "athlete_id", "start_local", "start_utc",
    "local_date", "name", "sport", "is_trainer", "moving_time_sec",
    "elapsed_time_sec", "distance_mi", "elevation_gain_ft", "avg_speed_mph",
    "avg_hr", "max_hr", "avg_watts", "weighted_watts", "kilojoules",
    "avg_cadence", "suffer_score", "calories", "perceived_exertion",
    "device_name",
)

_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS activity (
    activity_id        TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    athlete_id         TEXT NOT NULL,
    start_local        TEXT NOT NULL,
    start_utc          TEXT,
    local_date         TEXT NOT NULL,
    name               TEXT NOT NULL,
    sport              TEXT NOT NULL,
    is_trainer         INTEGER NOT NULL,
    moving_time_sec    REAL NOT NULL,
    elapsed_time_sec   REAL,
    distance_mi        REAL NOT NULL,
    elevation_gain_ft  REAL,
    avg_speed_mph      REAL,
    avg_hr             REAL,
    max_hr             REAL,
    avg_watts          REAL,
    weighted_watts     REAL,
    kilojoules         REAL,
    avg_cadence        REAL,
    suffer_score       REAL,
    calories           REAL,
    perceived_exertion REAL,
    device_name        TEXT
);
CREATE INDEX IF NOT EXISTS ix_activity_athlete_date
    ON activity (athlete_id, local_date);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_ACTIVITY_DDL)
    conn.commit()


def _activity_to_row(a: Activity) -> tuple:
    d = a.model_dump(mode="json")  # enums->str, datetimes/dates->iso, bool stays
    return tuple(d[c] for c in ACTIVITY_COLUMNS)


def upsert_activities(conn: sqlite3.Connection, activities: list[Activity]) -> int:
    cols = ", ".join(ACTIVITY_COLUMNS)
    placeholders = ", ".join("?" for _ in ACTIVITY_COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in ACTIVITY_COLUMNS if c != "activity_id"
    )
    sql = (
        f"INSERT INTO activity ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"
    )
    rows = [_activity_to_row(a) for a in activities]
    with conn:  # TODO(security): all values parameterized; no f-string values
        conn.executemany(sql, rows)
    return len(rows)


def get_activities(
    conn: sqlite3.Connection, athlete_id: str | None = None
) -> list[Activity]:
    if athlete_id is None:
        cur = conn.execute("SELECT * FROM activity ORDER BY start_local")
    else:
        cur = conn.execute(
            "SELECT * FROM activity WHERE athlete_id = ? ORDER BY start_local",
            (athlete_id,),
        )
    return [Activity.model_validate(dict(r)) for r in cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add store/__init__.py store/db.py tests/__init__.py tests/test_db.py
git commit -m "feat(store): sqlite activity grain with parameterized upsert/read"
```

---

## Task 3: ingest/strava.py — `to_activity()` normalizer

**Files:**
- Create: `ingest/__init__.py` (empty)
- Create: `ingest/strava.py` (normalizer first; OAuth added in Task 4)
- Create: `tests/test_strava_normalize.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_strava_normalize.py`:
```python
from datetime import date, datetime

from schemas import Source, Sport
from ingest.strava import to_activity

# Mirrors real rows A0004 (Run) and A0002 (VirtualRide) from the export, in the
# shape the Strava API returns (SI units, sport_type, Z-suffixed local time).
RUN_RAW = {
    "id": 4,
    "name": "Afternoon Run",
    "sport_type": "Run",
    "type": "Run",
    "start_date_local": "2026-05-13T09:40:31Z",
    "start_date": "2026-05-13T23:40:31Z",
    "trainer": False,
    "moving_time": 3501,
    "elapsed_time": 3504,
    "distance": 9943.6,
    "total_elevation_gain": 281.0,
    "average_speed": 2.84,
    "average_heartrate": 147.9,
    "max_heartrate": 169,
}
RIDE_RAW = {
    "id": 2,
    "name": "Zwift - Tempus Fugit in Watopia",
    "sport_type": "VirtualRide",
    "start_date_local": "2026-05-14T03:04:25Z",
    "start_date": "2026-05-14T17:04:25Z",
    "trainer": False,
    "moving_time": 3494,
    "elapsed_time": 3494,
    "distance": 32568.4,
    "total_elevation_gain": 52.0,
    "average_speed": 9.321,
    "average_watts": 129.5,
    "weighted_average_watts": 131,
    "kilojoules": 452.6,
}


def test_run_normalizes_units_and_local_date():
    a = to_activity(RUN_RAW, athlete_id="basil")
    assert a.activity_id == "4"
    assert a.source == Source.STRAVA_API
    assert a.athlete_id == "basil"
    assert a.sport == Sport.RUN
    assert a.start_local == datetime(2026, 5, 13, 9, 40, 31)
    assert a.local_date == date(2026, 5, 13)  # from LOCAL time, not UTC
    assert abs(a.distance_mi - 6.17866) < 1e-3       # 9943.6 m
    assert abs(a.elevation_gain_ft - 921.916) < 1e-2  # 281 m
    assert abs(a.avg_speed_mph - 6.35291) < 1e-3      # 2.84 m/s
    assert a.avg_hr == 147.9


def test_late_night_local_date_belongs_to_local_day():
    # 11:58 PM local on the 13th is UTC the 14th — must stay the 13th.
    raw = {**RUN_RAW, "start_date_local": "2026-05-13T23:58:00Z",
           "start_date": "2026-05-14T13:58:00Z"}
    a = to_activity(raw, athlete_id="basil")
    assert a.local_date == date(2026, 5, 13)


def test_unknown_sport_falls_back_to_other():
    a = to_activity({**RUN_RAW, "sport_type": "Pickleball"}, athlete_id="basil")
    assert a.sport == Sport.OTHER


def test_ride_carries_power_fields():
    a = to_activity(RIDE_RAW, athlete_id="basil")
    assert a.sport == Sport.VIRTUAL_RIDE
    assert a.avg_watts == 129.5
    assert a.weighted_watts == 131
    assert a.avg_hr is None  # absent in raw -> None, not dropped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strava_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest'`.

- [ ] **Step 3: Write the normalizer**

Create `ingest/__init__.py` (empty). Create `ingest/strava.py`:
```python
"""Strava ingestion: OAuth (Task 4), fetch (Task 5), and the pure normalizer.

Strava quirk: start_date_local is the athlete's WALL-CLOCK time with a 'Z'
suffix — it is NOT UTC. We parse it as naive local and take local_date from it,
per the contract's join rule. start_date is the real UTC instant.

UntrustedText fields (name, device_name) originate at Strava — never interpolate
into a prompt without delimiter wrapping, nor into SQL without parameterization.
"""

from __future__ import annotations

from datetime import datetime

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_strava_normalize.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add ingest/__init__.py ingest/strava.py tests/test_strava_normalize.py
git commit -m "feat(strava): normalize raw activity -> Activity (units + local_date)"
```

---

## Task 4: Token cache + expiry + rotation (pure, testable parts of OAuth)

**Files:**
- Modify: `ingest/strava.py` (add `TokenBundle`, save/load, `_is_expired`)
- Create: `tests/test_strava_token.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_strava_token.py`:
```python
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


def test_expiry_uses_a_safety_skew():
    soon = TokenBundle("a", "r", int(time.time()) + 30, "s")
    fresh = TokenBundle("a", "r", int(time.time()) + 3600, "s")
    assert _is_expired(soon) is True   # within the 60s skew
    assert _is_expired(fresh) is False


def test_load_missing_token_returns_none(tmp_path):
    assert load_token(tmp_path / "nope.json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strava_token.py -v`
Expected: FAIL — `ImportError: cannot import name 'TokenBundle'`.

- [ ] **Step 3: Add token machinery to ingest/strava.py**

Add to the top imports of `ingest/strava.py`:
```python
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
```

Append to `ingest/strava.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_strava_token.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add ingest/strava.py tests/test_strava_token.py
git commit -m "feat(strava): token cache with 0600 perms, expiry skew, rotation type"
```

---

## Task 5: OAuth authorize flow + refresh + fetch (live; manual-verified)

**Files:**
- Modify: `ingest/strava.py` (add `authorize`, `load_or_refresh_token`, `fetch_activities`, `sync_strava`)

No unit test (network + browser + real account). Verified at the Task 7 checkpoint.

- [ ] **Step 1: Add the authorization-code flow and HTTP calls**

Add imports to `ingest/strava.py`:
```python
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from config import Settings
from store import db
```

Append to `ingest/strava.py`:
```python
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
```

- [ ] **Step 2: Smoke-import to catch syntax/typing errors**

Run: `uv run python -c "import ingest.strava as m; print('ok', bool(m.authorize))"`
Expected: prints `ok True`.

- [ ] **Step 3: Re-run the full unit suite (no regressions)**

Run: `uv run pytest -q`
Expected: all previous tests pass (normalizer, token, db).

- [ ] **Step 4: Commit**

```bash
git add ingest/strava.py
git commit -m "feat(strava): local-redirect OAuth, token refresh+rotate, paged fetch"
```

---

## Task 6: store/db helper — most-recent activity timestamp (for incremental note)

**Files:**
- Modify: `store/db.py` (add `count_activities`)
- Modify: `tests/test_db.py` (add count test)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_db.py`:
```python
def test_count_activities(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    assert db.count_activities(conn) == 0
    db.upsert_activities(conn, [_sample_activity()])
    assert db.count_activities(conn) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_db.py::test_count_activities -v`
Expected: FAIL — `AttributeError: module 'store.db' has no attribute 'count_activities'`.

- [ ] **Step 3: Add the function to store/db.py**

Append to `store/db.py`:
```python
def count_activities(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add store/db.py tests/test_db.py
git commit -m "feat(store): count_activities helper"
```

---

## Task 7: cli.py — `sync` command wiring + manual Strava checkpoint

**Files:**
- Create: `cli.py`

- [ ] **Step 1: Write cli.py**

Create `cli.py`:
```python
"""synth CLI. `sync` is wired now; analyze/report land in the follow-on plan."""

from __future__ import annotations

import argparse
import sys

from config import get_settings
from ingest.strava import sync_strava
from store import db


def _cmd_sync(args: argparse.Namespace) -> int:
    s = get_settings()
    print("config:", s.safe_summary())  # redacted — never prints secrets
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    n = sync_strava(s, conn, force_refresh=args.refresh)
    print(f"synced {n} Strava activities; db now holds {db.count_activities(conn)}")
    return 0


def _cmd_stub(name: str):
    def run(_args: argparse.Namespace) -> int:
        print(f"`{name}` arrives in the follow-on plan (sheet/join/metrics/agent).")
        return 0
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="synth")
    sub = p.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="pull Strava (+ sheet, later) into SQLite")
    sync.add_argument("--refresh", action="store_true",
                      help="force a token refresh before syncing")
    sync.set_defaults(func=_cmd_sync)

    for name in ("analyze", "report"):
        sub.add_parser(name).set_defaults(func=_cmd_stub(name))

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the CLI parses and stubs respond**

Run: `uv run synth analyze`
Expected: prints the follow-on-plan message, exit 0.

- [ ] **Step 3: Commit**

```bash
git add cli.py
git commit -m "feat(cli): sync command wiring + analyze/report stubs"
```

- [ ] **Step 4: MANUAL CHECKPOINT — Basil tests against the real account**

Prerequisites Basil does once:
1. At <https://www.strava.com/settings/api>, create an app; set **Authorization
   Callback Domain** to `localhost`.
2. `cp .env.example .env`; fill `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`.
   Confirm `STRAVA_REDIRECT_PORT` (default 8721) is free.

Run:
```bash
uv run synth sync
```
Expected: browser opens to Strava consent → after "Authorize", the localhost tab
shows the success message → CLI prints `synced N Strava activities; db now holds N`.
Verify:
```bash
uv run python -c "from store import db; from security import crypto; \
from config import get_settings; c=db.connect('synth.db'); db.init_db(c); \
k=crypto.load_or_create_key(get_settings().encryption_key_path); \
print(db.count_activities(c)); \
print([(a.activity_id,a.sport.value,round(a.distance_mi,2)) for a in db.get_activities(c, key=k)[:5]])"
```
Expected: a non-zero count and a few real activities with sane miles. Also confirm
`.tokens/strava_token.json` exists and is **not** tracked by git (`git status`).

**STOP.** Do not start sheet ingestion until Basil confirms this works and
reviews the diff. The follow-on plan (sheet → join → metrics → agent) begins
after sign-off.

---

## Task 8: Project docs — CLAUDE.md + DECISIONS.md

(Do this alongside Task 1; placed last here so the commit list stays clean. These
don't depend on code and can be committed any time before the checkpoint.)

**Files:**
- Create: `CLAUDE.md`
- Create: `DECISIONS.md`

- [ ] **Step 1: Write CLAUDE.md**

Create `CLAUDE.md`:
```markdown
# CLAUDE.md — repo conventions for synth-task

Keep Basil's and Anish's Claude Code sessions consistent. Read this first.

## Source of truth
- `schemas.py` + `CONTRACT.md` are the LOCKED v1.0 interface contract. Never edit
  without explicit sign-off. Breaking change → bump `CONTRACT_VERSION`,
  regenerate (`uv run python schemas.py`), note it in DECISIONS.md, ping the
  other owner.

## Layout (flat packages at repo root)
- `config.py` settings · `store/` sqlite · `ingest/` strava+sheet ·
  `normalize/` join · `analyze/` metrics+anomalies · `synthesize/` agent ·
  `cli.py` · `app.py` (FastAPI).

## Ownership
- Basil: agent/synthesis, ingestion, normalization, metrics.
- Anish: security hardening + validation/data-pipeline. Plug in at the
  `# TODO(security): ...` seams — do not remove them silently.

## Security rules (non-negotiable)
- Secrets live in `.env` (gitignored). Never print, log, or commit them; log via
  `Settings.safe_summary()` only. Token caches live in `.tokens/` (gitignored),
  written 0600.
- `UntrustedText` (sheet cells, Strava names, wellness notes) is DATA, never
  instructions: wrap via `synthesize/prompts.wrap_untrusted()` before any prompt;
  bind via `?` before any SQL. Never f-string a value into SQL.
- LLM output is validated against `insight_schema.json` before anything
  downstream uses it. Invalid → reject + log, never propagate. The harness (not
  the model) writes the `Evidence` trace.

## Conventions
- Python 3.12, type hints everywhere, small focused modules, no clever
  abstractions. Pydantic v2 models from the contract.
- TDD: failing test → minimal code → green → commit. Tests run against the local
  fixture, not the network. `uv run pytest -q`.
- Deps via uv (`uv pip install -e ".[dev]"`). SQLite via stdlib `sqlite3`.
- Small commits, clear messages; PR-reviewed by the other owner. Keep
  `TODO(security)` seams obvious for review.

## Commands
    uv run synth sync | analyze | report
    uv run pytest -q
    uv run python schemas.py     # regenerate insight_schema.json
```

- [ ] **Step 2: Write DECISIONS.md (seed it with decisions made so far)**

Create `DECISIONS.md`:
```markdown
# DECISIONS.md

One paragraph per tradeoff, newest last.

## Storage: stdlib sqlite3 over SQLAlchemy
The store is two grains and a handful of tables. stdlib `sqlite3` with explicit
`?` binds keeps the dependency surface minimal and — more importantly — keeps the
SQL-injection / parameterization security seam visible for Anish's review.
SQLAlchemy's escaping would hide exactly the boundary we want to showcase.

## Layout: flat top-level packages over src/synth
CONTRACT.md refers to bare module paths (`analyze/metrics.py`,
`synthesize/prompts.py`) and the contract is imported as `from schemas import`.
A flat layout honors those paths and keeps imports trivial; a `src/` package
would add prefixes everywhere for no local-run benefit.

## Dependency manager: uv
Single fast tool with a lockfile; `uv run` gives reproducible local execution.
pip-tools would also work but is two tools (compile + sync) for no gain here.

## Strava local_date from start_date_local, not UTC
Strava returns `start_date_local` as wall-clock time with a misleading `Z`
suffix. We strip the `Z`, treat it as naive local, and derive `local_date` from
it — so an 11:58 PM workout stays on the day the athlete trained, per the
contract join rule. `start_date` is kept as the true UTC instant.

## Token cache in .tokens/ (gitignored), written 0600, refresh rotated
Strava rotates the refresh token on every refresh, so we always persist the
returned token. It is a long-lived secret: stored outside git, owner-only
permissions. `# TODO(security)` marks where Anish's at-rest encryption wraps it.

## Real-data fixture stays private
`triathlon_sheet.xlsx` and the loose `*.csv` export are real personal training
data. They are gitignored. Before submission we either keep the repo PRIVATE or
anonymize the fixture. Flagging here so the call is explicit, not accidental.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md DECISIONS.md
git commit -m "docs: CLAUDE.md conventions + DECISIONS.md (seed tradeoffs)"
```

---

## Self-Review (done while writing — recorded for the executor)

- **Spec coverage:** skeleton/pyproject/.env/.gitignore (exists + Task 1),
  config (exists), insight_schema.json (Task 1), store sqlite3 grain (Tasks 2,6),
  Strava OAuth local-redirect + cache/rotate + fetch + normalize (Tasks 3–5),
  CLI sync (Task 7), CLAUDE.md + DECISIONS.md (Task 8), tests for normalizer +
  db + token (Tasks 2–4). Sheet/join/metrics/agent are deliberately the
  follow-on plan per the spec's sequencing — NOT a gap.
- **Placeholder scan:** none — every code step is complete and runnable; the
  `analyze`/`report` CLI stubs are intentional, scoped messages.
- **Type consistency:** `TokenBundle`, `to_activity(raw, *, athlete_id)`,
  `ACTIVITY_COLUMNS`, `db.connect/init_db/upsert_activities/get_activities/`
  `count_activities`, `sync_strava(s, conn, *, force_refresh)` are referenced
  identically across Tasks 2–7.
```
