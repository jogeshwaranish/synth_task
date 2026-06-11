"""SQLite persistence. All values are bound via ? — never string-formatted.

security(Anish): field-level encryption at rest. The UntrustedText / PII free-text
columns (ENCRYPTED_COLUMNS) are AES-256-GCM encrypted via security/crypto.py
before they hit disk and decrypted on read; numeric metrics stay plaintext so
they remain queryable/indexable. Whole-file encryption (SQLCipher) was rejected
because it needs a non-stdlib driver, contradicting the stdlib-sqlite3 decision
(see DECISIONS.md). Pass `key=` to enable; the same per-machine key guards the
token cache.
Owners: Basil (schema + queries), Anish (encryption seam).
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from schemas import Activity, BikeSplit, RunSplit, SwimSplit, WellnessDay
from security import crypto

# Field order is the single source of truth for both the table and the binds.
ACTIVITY_COLUMNS: tuple[str, ...] = (
    "activity_id", "source", "athlete_id", "start_local", "start_utc",
    "local_date", "name", "sport", "is_trainer", "moving_time_sec",
    "elapsed_time_sec", "distance_mi", "elevation_gain_ft", "avg_speed_mph",
    "avg_hr", "max_hr", "avg_watts", "weighted_watts", "kilojoules",
    "avg_cadence", "suffer_score", "calories", "perceived_exertion",
    "device_name",
)

# Externally-authored free text (UntrustedText in the contract) — the PII /
# injection surface. Encrypted at rest when a key is supplied. Numeric metrics
# are deliberately left plaintext so the agent's queries can still filter on them.
ENCRYPTED_COLUMNS: tuple[str, ...] = ("name", "device_name")
_ENC_PREFIX = "enc:v1:"  # marks an encrypted cell; lets decrypt pass plaintext through

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

# Wellness grain: one row per athlete per local_date (the join key). `notes` is
# UntrustedText and the contract's PRIMARY prompt-injection surface — encrypted
# at rest like the activity PII columns, via the same per-machine key.
WELLNESS_COLUMNS: tuple[str, ...] = (
    "athlete_id", "local_date",
    "in_bed_hours", "asleep_hours", "snoring",
    "rhr", "hrv", "body_weight_lb", "sauna_mins",
    "notes",
)

ENCRYPTED_WELLNESS_COLUMNS: tuple[str, ...] = ("notes",)

_WELLNESS_DDL = """
CREATE TABLE IF NOT EXISTS wellness (
    athlete_id      TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    in_bed_hours    REAL,
    asleep_hours    REAL,
    snoring         REAL,
    rhr             REAL,
    hrv             REAL,
    body_weight_lb  REAL,
    sauna_mins      REAL,
    notes           TEXT,
    PRIMARY KEY (athlete_id, local_date)
);
"""


# Per-activity split grain (agent drill-down, CONTRACT.md). PK (activity_id,
# split_index). SwimSplit.stroke_style is UntrustedText -> encrypted at rest.
RUN_SPLIT_COLUMNS: tuple[str, ...] = (
    "activity_id", "split_index", "distance_mi", "moving_time_sec",
    "pace_min_per_mi", "avg_hr", "max_hr", "avg_cadence_run",
    "elevation_gain_ft", "is_partial",
)
BIKE_SPLIT_COLUMNS: tuple[str, ...] = (
    "activity_id", "split_index", "duration_sec", "distance_mi", "avg_speed_mph",
    "avg_hr", "avg_power", "avg_cadence", "elevation_gain_ft", "is_partial",
)
SWIM_SPLIT_COLUMNS: tuple[str, ...] = (
    "activity_id", "split_index", "swim_context", "distance", "distance_unit",
    "duration_sec", "pace_sec_per_100", "stroke_style", "swolf", "avg_hr",
)
ENCRYPTED_SWIM_SPLIT_COLUMNS: tuple[str, ...] = ("stroke_style",)

_SPLITS_DDL = """
CREATE TABLE IF NOT EXISTS run_split (
    activity_id        TEXT NOT NULL,
    split_index        INTEGER NOT NULL,
    distance_mi        REAL NOT NULL,
    moving_time_sec    REAL NOT NULL,
    pace_min_per_mi    REAL,
    avg_hr             REAL,
    max_hr             REAL,
    avg_cadence_run    REAL,
    elevation_gain_ft  REAL,
    is_partial         INTEGER NOT NULL,
    PRIMARY KEY (activity_id, split_index)
);
CREATE TABLE IF NOT EXISTS bike_split (
    activity_id        TEXT NOT NULL,
    split_index        INTEGER NOT NULL,
    duration_sec       REAL NOT NULL,
    distance_mi        REAL NOT NULL,
    avg_speed_mph      REAL,
    avg_hr             REAL,
    avg_power          REAL,
    avg_cadence        REAL,
    elevation_gain_ft  REAL,
    is_partial         INTEGER NOT NULL,
    PRIMARY KEY (activity_id, split_index)
);
CREATE TABLE IF NOT EXISTS swim_split (
    activity_id        TEXT NOT NULL,
    split_index        INTEGER NOT NULL,
    swim_context       TEXT,
    distance           REAL NOT NULL,
    distance_unit      TEXT NOT NULL,
    duration_sec       REAL NOT NULL,
    pace_sec_per_100   REAL,
    stroke_style       TEXT,
    swolf              REAL,
    avg_hr             REAL,
    PRIMARY KEY (activity_id, split_index)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # executescript() issues an implicit COMMIT; do not call mid-transaction.
    conn.executescript(_ACTIVITY_DDL + _WELLNESS_DDL + _SPLITS_DDL)


def _encrypt_field(value: str | None, key: bytes) -> str | None:
    if value is None:
        return None
    blob = crypto.encrypt(value.encode("utf-8"), key)
    return _ENC_PREFIX + base64.b64encode(blob).decode("ascii")


def _decrypt_field(value: str | None, key: bytes) -> str | None:
    # Plaintext / pre-encryption rows pass straight through (no prefix).
    if value is None or not value.startswith(_ENC_PREFIX):
        return value
    blob = base64.b64decode(value[len(_ENC_PREFIX):])
    return crypto.decrypt(blob, key).decode("utf-8")


def _activity_to_row(a: Activity, key: bytes | None) -> tuple[object, ...]:
    d = a.model_dump(mode="json")  # enums->str, datetimes/dates->iso str, bool->int 1/0 once in sqlite
    if key is not None:
        for c in ENCRYPTED_COLUMNS:
            d[c] = _encrypt_field(d[c], key)
    return tuple(d[c] for c in ACTIVITY_COLUMNS)


def upsert_activities(
    conn: sqlite3.Connection, activities: list[Activity], *, key: bytes | None = None
) -> int:
    cols = ", ".join(ACTIVITY_COLUMNS)
    placeholders = ", ".join("?" for _ in ACTIVITY_COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in ACTIVITY_COLUMNS if c != "activity_id"
    )
    sql = (
        f"INSERT INTO activity ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"
    )
    rows = [_activity_to_row(a, key) for a in activities]
    with conn:  # security: all values parameterized; no f-string values
        conn.executemany(sql, rows)
    return len(rows)


def get_activities(
    conn: sqlite3.Connection, athlete_id: str | None = None, *, key: bytes | None = None
) -> list[Activity]:
    if athlete_id is None:
        cur = conn.execute("SELECT * FROM activity ORDER BY start_local")
    else:
        cur = conn.execute(
            "SELECT * FROM activity WHERE athlete_id = ? ORDER BY start_local",
            (athlete_id,),
        )
    out: list[Activity] = []
    for r in cur.fetchall():
        d = dict(r)
        if key is not None:
            for c in ENCRYPTED_COLUMNS:
                d[c] = _decrypt_field(d[c], key)
        out.append(Activity.model_validate(d))
    return out


def count_activities(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]


def upsert_wellness(
    conn: sqlite3.Connection, days: list[WellnessDay], *, key: bytes | None = None
) -> int:
    cols = ", ".join(WELLNESS_COLUMNS)
    placeholders = ", ".join("?" for _ in WELLNESS_COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in WELLNESS_COLUMNS
        if c not in ("athlete_id", "local_date")
    )
    sql = (
        f"INSERT INTO wellness ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(athlete_id, local_date) DO UPDATE SET {updates}"
    )
    rows = []
    for w in days:
        d = w.model_dump(mode="json")
        if key is not None:
            for c in ENCRYPTED_WELLNESS_COLUMNS:
                d[c] = _encrypt_field(d[c], key)
        rows.append(tuple(d[c] for c in WELLNESS_COLUMNS))
    with conn:  # security: all values parameterized; no f-string values
        conn.executemany(sql, rows)
    return len(rows)


def get_wellness(
    conn: sqlite3.Connection, athlete_id: str | None = None, *, key: bytes | None = None
) -> list[WellnessDay]:
    if athlete_id is None:
        cur = conn.execute("SELECT * FROM wellness ORDER BY local_date")
    else:
        cur = conn.execute(
            "SELECT * FROM wellness WHERE athlete_id = ? ORDER BY local_date",
            (athlete_id,),
        )
    out: list[WellnessDay] = []
    for r in cur.fetchall():
        d = dict(r)
        if key is not None:
            for c in ENCRYPTED_WELLNESS_COLUMNS:
                d[c] = _decrypt_field(d[c], key)
        out.append(WellnessDay.model_validate(d))
    return out


# --- per-activity splits (shared shape: PK (activity_id, split_index)) ------

def _upsert_split(conn, table, columns, models, *, key=None, encrypted=()):
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in columns if c not in ("activity_id", "split_index")
    )
    sql = (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(activity_id, split_index) DO UPDATE SET {updates}"
    )
    rows = []
    for m in models:
        d = m.model_dump(mode="json")
        if key is not None:
            for c in encrypted:
                d[c] = _encrypt_field(d[c], key)
        rows.append(tuple(d[c] for c in columns))
    with conn:  # security: all values parameterized; table/columns are constants
        conn.executemany(sql, rows)
    return len(rows)


def _get_splits(conn, table, model, *, activity_id=None, key=None, encrypted=()):
    if activity_id is None:
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY activity_id, split_index")
    else:
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE activity_id = ? ORDER BY split_index",
            (activity_id,),
        )
    out = []
    for r in cur.fetchall():
        d = dict(r)
        if key is not None:
            for c in encrypted:
                d[c] = _decrypt_field(d[c], key)
        out.append(model.model_validate(d))
    return out


def upsert_run_splits(conn, splits: list[RunSplit]) -> int:
    return _upsert_split(conn, "run_split", RUN_SPLIT_COLUMNS, splits)


def upsert_bike_splits(conn, splits: list[BikeSplit]) -> int:
    return _upsert_split(conn, "bike_split", BIKE_SPLIT_COLUMNS, splits)


def upsert_swim_splits(conn, splits: list[SwimSplit], *, key: bytes | None = None) -> int:
    return _upsert_split(conn, "swim_split", SWIM_SPLIT_COLUMNS, splits,
                         key=key, encrypted=ENCRYPTED_SWIM_SPLIT_COLUMNS)


def get_run_splits(conn, activity_id: str | None = None) -> list[RunSplit]:
    return _get_splits(conn, "run_split", RunSplit, activity_id=activity_id)


def get_bike_splits(conn, activity_id: str | None = None) -> list[BikeSplit]:
    return _get_splits(conn, "bike_split", BikeSplit, activity_id=activity_id)


def get_swim_splits(
    conn, activity_id: str | None = None, *, key: bytes | None = None
) -> list[SwimSplit]:
    return _get_splits(conn, "swim_split", SwimSplit, activity_id=activity_id,
                       key=key, encrypted=ENCRYPTED_SWIM_SPLIT_COLUMNS)
