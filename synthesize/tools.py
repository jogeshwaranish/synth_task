"""Read-only investigation tools for the synthesis agent. Owner: Basil.

Pure functions over the SQLite store — no model, no network — so the loop in
agent.py (and tests) can exercise them deterministically. Each returns a
JSON-serializable value. UntrustedText (activity name/device, swim stroke) is
fenced via synthesize.prompts.wrap_untrusted before it leaves a tool, because a
tool result is fed straight back to the model as content.
"""

from __future__ import annotations

import json
from datetime import date

from store import db
from synthesize.prompts import wrap_untrusted

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "query_anomalies",
        "description": (
            "List the deterministic anomalies (the investigation worklist). "
            "Optionally filter by severity. Descriptions are authored by trusted "
            "code, not by you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["info", "watch", "flag"]}
            },
            "required": [],
        },
    },
    {
        "name": "get_daily_metrics",
        "description": (
            "Daily training-load metrics (acute/chronic load, ACWR, load z-score, "
            "pace and HR-at-pace trends) over an inclusive date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_start": {"type": "string", "description": "YYYY-MM-DD"},
                "date_end": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["date_start", "date_end"],
        },
    },
    {
        "name": "get_activity_detail",
        "description": (
            "Open one activity and its per-split breakdown (run/bike/swim), e.g. "
            "to tell a deliberate interval session from real fatigue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"activity_id": {"type": "string"}},
            "required": ["activity_id"],
        },
    },
    {
        "name": "compare_periods",
        "description": (
            "Aggregate load and volume for two date ranges and return their "
            "deltas (this block vs a prior one)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
            },
            "required": [
                "period_a_start", "period_a_end",
                "period_b_start", "period_b_end",
            ],
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


def query_anomalies(conn, *, severity: str | None = None) -> list[dict]:
    rows = db.get_anomalies(conn)
    if severity is not None:
        rows = [a for a in rows if a.severity.value == severity]
    return [a.model_dump(mode="json") for a in rows]


def get_daily_metrics(
    conn, athlete_id: str, *, date_start: str, date_end: str
) -> list[dict]:
    lo, hi = date.fromisoformat(date_start), date.fromisoformat(date_end)
    return [
        m.model_dump(mode="json")
        for m in db.get_metrics(conn, athlete_id=athlete_id)
        if lo <= m.local_date <= hi
    ]
