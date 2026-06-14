"""Build the rowing test DB: one chosen rower, two fused sources.

Not part of the app. A throwaway harness for AG's new ask — prove the app can
(1) ingest a workbook whose schema is NOTHING like ours via the AI fallback,
(2) isolate ONE athlete out of ~40, and (3) fuse that with simulated training +
recovery to surface a coaching pattern.

Source A (real, dissimilar): the rowing-erg workbook. We ingest ONE athlete's
erg test results across the season via ingest/rowing.py (LLM infers the pivoted
layout once, then it's cached; identity canonicalised against the roster).

Source B (simulated, deterministic): daily cross-training + wellness for the
same athlete, with a PLANTED late-season pattern — training load ramps hard
from late January while HRV sinks, RHR climbs and sleep dips. It lines up with
what the erg data already shows (no 2x6k PR after early Feb): the athlete is
training through fatigue and her erg gains have flattened — non-functional
overreaching that only the FUSION of the two sources reveals.

Usage:  uv run python scripts/gen_rowing_test.py [db_path]
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from config import get_settings
from ingest import sheet
from ingest.rowing import ingest_rowing
from schemas import Activity, Source, Sport, WellnessDay
from security import crypto
from store import db

WORKBOOK = Path("rowing_women_2025-2026 ERGS-2.xlsx")
ATHLETE_QUERY = "Banks, Claire"
ATHLETE_ID = "banks_claire"
START = date(2025, 9, 8)        # first erg session in the workbook
END = date(2026, 3, 16)         # last erg session (a 2k)
RNG = random.Random(42)


# ---- phase model: when the planted overload happens -------------------------

def _phase(d: date) -> str:
    if d < date(2025, 12, 1):
        return "base"        # gentle ramp, healthy recovery, erg improving
    if d < date(2026, 1, 21):
        return "peak"        # fittest block; her erg bests land mid-January
    return "overload"        # load spikes, recovery markers sink, erg plateaus


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _load_factor(d: date) -> float:
    """Scales session duration -> training load -> ACWR."""
    ph = _phase(d)
    if ph == "base":
        return _lerp(0.85, 1.05, (d - START).days / (date(2025, 12, 1) - START).days)
    if ph == "peak":
        return 1.10
    t = (d - date(2026, 1, 21)).days / (END - date(2026, 1, 21)).days
    base = _lerp(1.15, 1.60, t)
    # a deliberate overload block in late Feb -> early Mar pushes ACWR to a flag
    if date(2026, 2, 23) <= d <= date(2026, 3, 6):
        base *= 1.25
    return base


def _hrv(d: date) -> float:
    """ms. Stable ~62, then suppressed through the overload (leads the stall)."""
    if _phase(d) != "overload":
        return 62.0 + RNG.uniform(-2, 2)
    t = (d - date(2026, 1, 21)).days / (END - date(2026, 1, 21)).days
    return _lerp(60.0, 46.0, t) + RNG.uniform(-1.5, 1.5)


def _rhr(d: date) -> float:
    """bpm. Mirror of HRV: climbs through the overload."""
    if _phase(d) != "overload":
        return 46.0 + RNG.uniform(-1, 1)
    t = (d - date(2026, 1, 21)).days / (END - date(2026, 1, 21)).days
    return _lerp(47.0, 56.0, t) + RNG.uniform(-1, 1)


def _sleep(d: date) -> float:
    if _phase(d) == "overload":
        return RNG.uniform(6.1, 6.9)
    return RNG.uniform(7.2, 8.1)


# ---- simulated cross-training (the erg TESTS come from the sheet) -----------

def _run(d: date, miles: float, label: str, hour: int, seq: int) -> Activity:
    factor = _load_factor(d)
    pace = 8.6 + RNG.uniform(-0.2, 0.2)
    mi = round(miles * factor, 2)
    secs = round(mi * pace * 60)
    hr = RNG.uniform(146, 156)
    return Activity(
        activity_id=f"S{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE_ID,
        start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))), local_date=d,
        name=label, sport=Sport.RUN, moving_time_sec=float(secs), distance_mi=mi,
        avg_speed_mph=round(60.0 / pace, 2), avg_hr=round(hr, 1),
        max_hr=round(hr + RNG.uniform(8, 16), 1), avg_cadence=round(RNG.uniform(84, 90), 1),
        suffer_score=round(secs / 60 * (hr / 150), 1))


def _bike(d: date, minutes: float, label: str, hour: int, seq: int) -> Activity:
    mins = minutes * _load_factor(d)
    hr = RNG.uniform(124, 136)
    speed = RNG.uniform(17, 20)
    watts = RNG.uniform(140, 175)
    return Activity(
        activity_id=f"S{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE_ID,
        start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))), local_date=d,
        name=label, sport=Sport.RIDE, moving_time_sec=round(mins * 60),
        distance_mi=round(mins / 60 * speed, 2), avg_speed_mph=round(speed, 2),
        avg_hr=round(hr, 1), max_hr=round(hr + RNG.uniform(10, 20), 1),
        avg_watts=round(watts, 1), suffer_score=round(mins * 0.8, 1))


def _lift(d: date, minutes: float, label: str, hour: int, seq: int) -> Activity:
    mins = minutes * _load_factor(d)
    return Activity(
        activity_id=f"S{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE_ID,
        start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))), local_date=d,
        name=label, sport=Sport.STRENGTH, moving_time_sec=round(mins * 60),
        distance_mi=0.0, suffer_score=round(mins * 0.5, 1))


def _sessions_for(d: date, seq: int) -> list[Activity]:
    wd = d.weekday()  # Mon=0
    if wd == 0:
        return [_run(d, 4.0, "Morning shakeout", 6, seq)]
    if wd == 1:
        return [_bike(d, 60, "Aerobic spin", 17, seq)]
    if wd == 2:
        return [_run(d, 5.0, "Tempo run", 7, seq), _lift(d, 45, "Lift", 16, seq + 1)]
    if wd == 3:
        return [_bike(d, 75, "Long aerobic ride", 9, seq)]
    if wd == 4:
        return []  # rest
    if wd == 5:
        return [_run(d, 7.0, "Long run", 8, seq)]
    return [_lift(d, 50, "Strength + core", 10, seq)]  # Sun


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("rowing_test.db")
    if db_path.exists():
        db_path.unlink()
    s = get_settings()
    key = crypto.load_or_create_key(s.encryption_key_path)
    conn = db.connect(db_path)
    db.init_db(conn)

    # --- Source A: ingest one rower's erg tests via the AI fallback ----------
    tabs = sheet._tabs_preview(WORKBOOK)
    erg = ingest_rowing(
        tabs, lambda tab: sheet._rows_from_xlsx(WORKBOOK, tab),
        settings=s, key=key, athlete_query=ATHLETE_QUERY, athlete_id=ATHLETE_ID)
    db.upsert_activities(conn, erg, key=key)

    # --- Source B: simulated daily cross-training + wellness -----------------
    activities: list[Activity] = []
    wellness: list[WellnessDay] = []
    seq, d = 1, START
    while d <= END:
        for a in _sessions_for(d, seq):
            activities.append(a)
            seq += 1
        wellness.append(WellnessDay(
            local_date=d, athlete_id=ATHLETE_ID, asleep_hours=round(_sleep(d), 1),
            in_bed_hours=round(_sleep(d) + RNG.uniform(0.3, 0.8), 1),
            rhr=round(_rhr(d), 1), hrv=round(_hrv(d), 1),
            body_weight_lb=round(150 + RNG.uniform(-1.5, 1.5), 1)))
        d += timedelta(days=1)
    db.upsert_activities(conn, activities, key=key)
    db.upsert_wellness(conn, wellness, key=key)
    conn.commit()

    print(f"rowing test DB -> {db_path}")
    print(f"  source A (erg sheet, AI-fallback): {len(erg)} sessions for {ATHLETE_QUERY!r}")
    print(f"  source B (simulated Strava): {len(activities)} activities + "
          f"{len(wellness)} wellness days  ({START}..{END})")


if __name__ == "__main__":
    main()
