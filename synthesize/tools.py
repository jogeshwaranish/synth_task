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


def get_activity_detail(conn, key, *, activity_id: str) -> dict:
    matches = [a for a in db.get_activities(conn, key=key)
               if a.activity_id == activity_id]
    if not matches:
        return {"error": f"no activity with id '{activity_id}'"}
    activity = matches[0].model_dump(mode="json")
    # Fence UntrustedText: it is fed straight back to the model as content.
    for field in ("name", "device_name"):
        if activity.get(field) is not None:
            activity[field] = wrap_untrusted(activity[field])

    swims = []
    for s in db.get_swim_splits(conn, activity_id, key=key):
        d = s.model_dump(mode="json")
        if d.get("stroke_style") is not None:
            d["stroke_style"] = wrap_untrusted(d["stroke_style"])
        swims.append(d)

    return {
        "activity": activity,
        "run_splits": [s.model_dump(mode="json")
                       for s in db.get_run_splits(conn, activity_id)],
        "bike_splits": [s.model_dump(mode="json")
                        for s in db.get_bike_splits(conn, activity_id)],
        "swim_splits": swims,
    }


def _mean_or_none(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _period_aggregate(conn, athlete_id: str, lo: date, hi: date) -> dict:
    metrics = [m for m in db.get_metrics(conn, athlete_id=athlete_id)
               if lo <= m.local_date <= hi]
    acute = [m.acute_load_7d for m in metrics if m.acute_load_7d is not None]
    acwr = [m.acwr for m in metrics if m.acwr is not None]
    return {
        "n_days": len(metrics),
        "mean_acute_load_7d": _mean_or_none(acute),
        "mean_acwr": _mean_or_none(acwr),
    }


def compare_periods(
    conn, athlete_id: str, key, *,
    period_a_start: str, period_a_end: str,
    period_b_start: str, period_b_end: str,
) -> dict:
    a = _period_aggregate(conn, athlete_id,
                          date.fromisoformat(period_a_start),
                          date.fromisoformat(period_a_end))
    b = _period_aggregate(conn, athlete_id,
                          date.fromisoformat(period_b_start),
                          date.fromisoformat(period_b_end))
    deltas = {
        k: (a[k] - b[k]) if a[k] is not None and b[k] is not None else None
        for k in ("mean_acute_load_7d", "mean_acwr")
    }
    return {"period_a": a, "period_b": b, "deltas": deltas}


def dispatch(conn, key, athlete_id: str, name: str, args: dict):
    if name == "query_anomalies":
        return query_anomalies(conn, severity=args.get("severity"))
    if name == "get_daily_metrics":
        return get_daily_metrics(conn, athlete_id, **args)
    if name == "get_activity_detail":
        return get_activity_detail(conn, key, **args)
    if name == "compare_periods":
        return compare_periods(conn, athlete_id, key, **args)
    return {"error": f"unknown tool '{name}'"}


def digest(name: str, result) -> str:
    if isinstance(result, list):
        body = f"{len(result)} rows"
    elif isinstance(result, dict) and "error" in result:
        body = result["error"]
    else:
        body = json.dumps(result, default=str)
    return f"{name} -> {body}"[:500]
