"""Pure per-day join: activities + wellness -> DailyRow. Owner: Basil.

Contract join rules (CONTRACT.md / DECISIONS.md):
- grain (athlete_id, local_date); local_date comes from start_local, never UTC
- sums for volume; duration-weighted means for intensity (weights only over
  activities where the metric is present); max for maxes
- missing wellness -> fields None, day NEVER dropped
- wellness-only days (rest days) still get a row — rest is signal

Computed on demand, never materialized: at this scale recomputation is instant
and there is no cache to invalidate on re-sync (see the 2026-06-10 design doc).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from schemas import Activity, DailyRow, Sport, WellnessDay

_RUN_SPORTS = {Sport.RUN, Sport.VIRTUAL_RUN}
_BIKE_SPORTS = {Sport.RIDE, Sport.VIRTUAL_RIDE}
_SWIM_SPORTS = {Sport.SWIM}


def _weighted_mean(pairs: list[tuple[float | None, float]]) -> float | None:
    present = [(v, w) for v, w in pairs if v is not None and w > 0]
    if not present:
        return None
    total_weight = sum(w for _, w in present)
    return sum(v * w for v, w in present) / total_weight


def _sum_or_none(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def build_daily_rows(
    activities: list[Activity], wellness_days: list[WellnessDay]
) -> list[DailyRow]:
    acts_by_key: dict[tuple[str, date], list[Activity]] = defaultdict(list)
    for a in activities:
        acts_by_key[(a.athlete_id, a.local_date)].append(a)
    wellness_by_key = {(w.athlete_id, w.local_date): w for w in wellness_days}

    out: list[DailyRow] = []
    for key in sorted(set(acts_by_key) | set(wellness_by_key)):
        athlete_id, local_date = key
        acts = sorted(acts_by_key.get(key, []), key=lambda a: a.start_local)
        runs = [a for a in acts if a.sport in _RUN_SPORTS]
        bikes = [a for a in acts if a.sport in _BIKE_SPORTS]
        run_minutes = sum(a.moving_time_sec for a in runs) / 60
        run_miles = sum(a.distance_mi for a in runs)
        out.append(DailyRow(
            local_date=local_date,
            athlete_id=athlete_id,
            source_mix=sorted({a.source for a in acts}, key=lambda s: s.value),
            session_count=len(acts),
            tri_session_count=sum(1 for a in acts if a.sport.is_tri),
            run_miles=run_miles,
            bike_miles=sum(a.distance_mi for a in bikes),
            swim_miles=sum(a.distance_mi for a in acts if a.sport in _SWIM_SPORTS),
            training_minutes=sum(a.moving_time_sec for a in acts) / 60,
            tri_training_minutes=sum(
                a.moving_time_sec for a in acts if a.sport.is_tri
            ) / 60,
            elevation_gain_ft=sum(a.elevation_gain_ft or 0 for a in acts),
            avg_hr=_weighted_mean([(a.avg_hr, a.moving_time_sec) for a in acts]),
            max_hr=max(
                (a.max_hr for a in acts if a.max_hr is not None), default=None
            ),
            avg_power_bike=_weighted_mean(
                [(a.avg_watts, a.moving_time_sec) for a in bikes]
            ),
            weighted_power_bike=_weighted_mean(
                [(a.weighted_watts, a.moving_time_sec) for a in bikes]
            ),
            avg_cadence_run=_weighted_mean(
                [(a.avg_cadence, a.moving_time_sec) for a in runs]
            ),
            avg_pace_run_min_per_mi=(
                run_minutes / run_miles if run_miles > 0 else None
            ),
            total_suffer_score=_sum_or_none([a.suffer_score for a in acts]),
            wellness=wellness_by_key.get(key),
            activity_ids=[a.activity_id for a in acts],
        ))
    return out
