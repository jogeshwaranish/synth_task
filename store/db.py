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

from schemas import Activity
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


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # executescript() issues an implicit COMMIT; do not call mid-transaction.
    conn.executescript(_ACTIVITY_DDL)


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
