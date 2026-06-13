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

from schemas import Anomaly, AnomalySeverity, DailyMetrics, DailyRow

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


def _half_mean(values: list[float | None], min_n: int) -> float | None:
    present = [v for v in values if v is not None]
    return _mean(present) if len(present) >= min_n else None


def _trend_pct(series14: list[float | None]) -> float | None:
    """Recent 7 calendar days vs the prior 7. Positive = value rising."""
    prior = _half_mean(series14[:7], MIN_TREND_DAYS_PER_HALF)
    recent = _half_mean(series14[7:], MIN_TREND_DAYS_PER_HALF)
    if prior is None or recent is None or prior == 0:
        return None
    return (recent - prior) / prior * 100


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
        pace = [rows[d].avg_pace_run_min_per_mi if d in rows else None for d in days]
        # HR-at-pace proxy: beats per mile (avg_hr [beats/min] * pace [min/mi]).
        beats_per_mi = [
            rows[d].avg_hr * rows[d].avg_pace_run_min_per_mi
            if d in rows and rows[d].avg_hr is not None
            and rows[d].avg_pace_run_min_per_mi is not None
            else None
            for d in days
        ]

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
                pace_trend_pct_14d=(
                    _trend_pct(pace[i - 13:i + 1]) if i >= 13 else None
                ),
                hr_at_pace_trend_pct_14d=(
                    _trend_pct(beats_per_mi[i - 13:i + 1]) if i >= 13 else None
                ),
                rest_day=minutes[i] == 0,
            ))
    return out


def _anomaly(athlete_id: str, d: date, metric: str, value: float,
             severity: AnomalySeverity, description: str, *,
             baseline: float | None = None,
             zscore: float | None = None) -> Anomaly:
    # Deterministic id -> re-running analyze upserts in place. The contract
    # Anomaly has no athlete_id field, so the id carries it.
    return Anomaly(
        anomaly_id=f"{athlete_id}:{d.isoformat()}:{metric}",
        local_date=d, metric=metric, value=value, baseline=baseline,
        zscore=zscore, severity=severity, description=description,
    )


def _acwr_anomaly(m: DailyMetrics) -> Anomaly | None:
    if m.acwr is None:
        return None
    if m.acwr > ACWR_FLAG:
        sev = AnomalySeverity.FLAG
    elif m.acwr > ACWR_HIGH or m.acwr < ACWR_LOW:
        sev = AnomalySeverity.WATCH
    else:
        return None
    direction = "above" if m.acwr > ACWR_HIGH else "below"
    desc = (
        f"ACWR {m.acwr:.2f} is {direction} the safe window "
        f"{ACWR_LOW}-{ACWR_HIGH} (acute {m.acute_load_7d:.0f} min vs "
        f"chronic {m.chronic_load_28d:.0f} min)."
    )
    return _anomaly(m.athlete_id, m.local_date, "acwr", m.acwr, sev, desc)


def _load_zscore_anomaly(m: DailyMetrics, row: DailyRow | None) -> Anomaly | None:
    z = m.load_zscore_28d
    if z is None or z <= ZSCORE_WATCH:       # high side only; low = ACWR's job
        return None
    sev = AnomalySeverity.FLAG if z > ZSCORE_FLAG else AnomalySeverity.WATCH
    minutes = row.training_minutes if row is not None else 0.0
    baseline = m.chronic_load_28d / 7 if m.chronic_load_28d is not None else None
    desc = (
        f"Single-day load {minutes:.0f} min is z={z:+.1f} vs the 28d daily "
        f"mean of {baseline:.0f} min."
    )
    return _anomaly(m.athlete_id, m.local_date, "load_zscore_28d", minutes,
                    sev, desc, baseline=baseline, zscore=z)


def detect_anomalies(
    daily_rows: list[DailyRow], metrics: list[DailyMetrics]
) -> list[Anomaly]:
    rows = {(r.athlete_id, r.local_date): r for r in daily_rows}
    out: list[Anomaly] = []
    for m in metrics:
        row = rows.get((m.athlete_id, m.local_date))
        for candidate in (_acwr_anomaly(m), _load_zscore_anomaly(m, row)):
            if candidate is not None:
                out.append(candidate)
        out.extend(_trend_anomalies(m))
    out.extend(_wellness_anomalies(daily_rows))
    return out


_TREND_DETECTORS = (
    ("pace_trend_pct_14d", "14d run pace slowed"),
    ("hr_at_pace_trend_pct_14d", "14d HR-at-pace (beats/mi) drifted up"),
)


def _trend_anomalies(m: DailyMetrics) -> list[Anomaly]:
    out = []
    for metric, label in _TREND_DETECTORS:
        value = getattr(m, metric)
        if value is None or value <= TREND_WATCH_PCT:
            continue
        sev = (AnomalySeverity.FLAG if value > TREND_FLAG_PCT
               else AnomalySeverity.WATCH)
        desc = f"{label} {value:+.1f}% vs the prior 7 days."
        out.append(_anomaly(m.athlete_id, m.local_date, metric, value, sev, desc))
    return out


# (field, direction, label): direction +1 fires on high z, -1 on low z.
_WELLNESS_DETECTORS = (
    ("rhr", 1, "elevated"),
    ("hrv", -1, "suppressed"),
)


def _wellness_anomalies(daily_rows: list[DailyRow]) -> list[Anomaly]:
    by_athlete: dict[str, list[DailyRow]] = defaultdict(list)
    for r in sorted(daily_rows, key=lambda r: (r.athlete_id, r.local_date)):
        if r.wellness is not None:
            by_athlete[r.athlete_id].append(r)

    out: list[Anomaly] = []
    for athlete_id, rows in by_athlete.items():
        for field, direction, label in _WELLNESS_DETECTORS:
            points = [(r.local_date, getattr(r.wellness, field))
                      for r in rows if getattr(r.wellness, field) is not None]
            for i, (d, value) in enumerate(points):
                window = [v for (dd, v) in points[:i]
                          if 0 < (d - dd).days <= WELLNESS_WINDOW_DAYS]
                if len(window) < MIN_WELLNESS_BASELINE_DAYS:
                    continue
                mean, sd = _mean(window), _std(window)
                if sd == 0:
                    continue
                z = (value - mean) / sd
                if direction * z >= ZSCORE_FLAG:
                    sev = AnomalySeverity.FLAG
                elif direction * z >= ZSCORE_WATCH:
                    sev = AnomalySeverity.WATCH
                else:
                    continue
                desc = (
                    f"{field.upper()} {value:.0f} is {label} vs its 28d "
                    f"baseline {mean:.0f} (z={z:+.1f})."
                )
                out.append(_anomaly(athlete_id, d, field, value, sev, desc,
                                    baseline=mean, zscore=z))
    return out
