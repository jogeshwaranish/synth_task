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
    # executescript() issues an implicit COMMIT; do not call mid-transaction.
    conn.executescript(_ACTIVITY_DDL)


def _activity_to_row(a: Activity) -> tuple[object, ...]:
    d = a.model_dump(mode="json")  # enums->str, datetimes/dates->iso str, bool->int 1/0 once in sqlite
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


def count_activities(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
