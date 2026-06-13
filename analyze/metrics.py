"""Deterministic training-load metrics + anomalies. Owner: Basil.

Pure functions — no I/O, no LLM. Every metric is relative to the athlete's OWN
rolling history (spec: docs/superpowers/specs/2026-06-09-synth-mvp-design.md).
DailyRows exist only for days with data, so windows run over a padded CALENDAR
series: a missing day is a real rest day (0 training minutes), not a hole.
Thresholds are tunable defaults, logged in DECISIONS.md.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from schemas import DailyMetrics, DailyRow

# --- tunable detector thresholds (DECISIONS.md) -----------------------------
ACWR_LOW = 0.8          # below = detraining (Gabbett sweet spot 0.8-1.3)
ACWR_HIGH = 1.3         # above = elevated injury risk
ACWR_FLAG = 1.5         # above = load-spike injury risk
ZSCORE_WATCH = 2.0
ZSCORE_FLAG = 3.0
TREND_WATCH_PCT = 5.0
TREND_FLAG_PCT = 10.0
MIN_TREND_DAYS_PER_HALF = 2     # run days needed in each 7d half-window
WELLNESS_WINDOW_DAYS = 28
MIN_WELLNESS_BASELINE_DAYS = 14


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5  # population std


def compute_metrics(daily_rows: list[DailyRow]) -> list[DailyMetrics]:
    by_athlete: dict[str, dict[date, DailyRow]] = defaultdict(dict)
    for r in daily_rows:
        by_athlete[r.athlete_id][r.local_date] = r

    out: list[DailyMetrics] = []
    for athlete_id in sorted(by_athlete):
        rows = by_athlete[athlete_id]
        first, last = min(rows), max(rows)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        minutes = [rows[d].training_minutes if d in rows else 0.0 for d in days]

        for i, d in enumerate(days):
            acute = sum(minutes[i - 6:i + 1]) if i >= 6 else None
            chronic = zscore = None
            if i >= 27:
                window = minutes[i - 27:i + 1]
                mean28 = _mean(window)
                chronic = mean28 * 7
                sd = _std(window)
                zscore = (minutes[i] - mean28) / sd if sd > 0 else None
            acwr = acute / chronic if acute is not None and chronic else None
            out.append(DailyMetrics(
                local_date=d,
                athlete_id=athlete_id,
                acute_load_7d=acute,
                chronic_load_28d=chronic,
                acwr=acwr,
                load_zscore_28d=zscore,
                rest_day=minutes[i] == 0,
            ))
    return out
