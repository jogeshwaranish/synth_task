"""Generate synthetic Strava test data with deliberately NON-OBVIOUS patterns.

Not part of the app. A throwaway harness to stress-test the synthesis agent:
does the coach surface insights that no single day reveals? Writes a fresh
test DB (default synth_test.db) so the real synth.db is never touched.

Three planted patterns (all calibrated to analyze/metrics.py thresholds):
  A. Masked aerobic decoupling - a "safe-looking" March build where run PACE is
     held flat but HR creeps up, so HR-at-pace drifts up with no single alarm.
  B. HRV leads the breakdown - HRV suppression / RHR elevation fire ~1 week
     BEFORE the HR-at-pace drift is visible (only findable by fusing sources).
  C. The ACWR paradox - the late-March LOW acwr (detraining) is the HEALTHY
     recovery week (markers rebound), while the prior normal acwr hid overreach.

Usage:  uv run python scripts/gen_test_strava.py [db_path]
"""

from __future__ import annotations

import math
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from schemas import Activity, Source, Sport, WellnessDay
from security import crypto
from store import db

ATHLETE = "anish"
START = date(2025, 12, 22)
END = date(2026, 5, 14)
RNG = random.Random(42)  # deterministic


# ---- phase model: each calendar day maps to a phase with its own knobs -------

def _phase(d: date) -> str:
    if d < date(2026, 1, 12):
        return "base"        # build a chronic baseline so later ratios mean something
    if d < date(2026, 2, 9):
        return "steady"      # flat healthy reference block
    if d < date(2026, 3, 30):
        return "overreach"   # the masked build (patterns A + B)
    if d < date(2026, 4, 13):
        return "recovery"    # forced easy week (pattern C - low acwr but healthy)
    return "rebuild"         # smart progression, genuine adaptation


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _run_pace(d: date) -> float:
    """min/mile. Held ~flat through the overreach (that's the whole point);
    genuinely improves only during the rebuild."""
    ph = _phase(d)
    if ph == "base":
        return 8.67
    if ph in ("steady", "overreach", "recovery"):
        return 8.58
    # rebuild: real fitness shows up as faster pace at the SAME hr
    t = (d - date(2026, 4, 13)).days / (END - date(2026, 4, 13)).days
    return _lerp(8.58, 8.30, t)


def _run_hr(d: date) -> float:
    """avg run HR. The hidden signal: it creeps up through the overreach while
    pace is flat (decoupling), then recovers when load is cut."""
    ph = _phase(d)
    if ph in ("base", "steady"):
        return 150.0
    if ph == "overreach":
        # flat early, then a clear upward creep from ~Mar 8 (AFTER hrv drops)
        if d < date(2026, 3, 8):
            return 151.0
        t = (d - date(2026, 3, 8)).days / (date(2026, 3, 29) - date(2026, 3, 8)).days
        return _lerp(151.0, 170.0, t)
    if ph == "recovery":
        return 149.0       # same pace, HR back to normal -> decoupling resolved
    return 150.0           # rebuild: HR steady while pace improves = adaptation


def _load_factor(d: date) -> float:
    """scales session duration/distance -> drives training load + acwr."""
    ph = _phase(d)
    if ph == "base":
        t = (d - START).days / (date(2026, 1, 12) - START).days
        return _lerp(0.80, 1.00, t)
    if ph == "steady":
        return 1.00
    if ph == "overreach":
        t = (d - date(2026, 2, 9)).days / (date(2026, 3, 30) - date(2026, 2, 9)).days
        return _lerp(1.00, 1.45, t)   # gradual ramp - never a single-day spike
    if ph == "recovery":
        return 0.50                   # deliberate cut -> acwr dips below 0.8
    t = (d - date(2026, 4, 13)).days / (END - date(2026, 4, 13)).days
    return _lerp(0.75, 1.10, t)


# ---- wellness: the recovery markers that LEAD the performance signal ----------

def _hrv(d: date) -> float:
    """ms. Stable ~64, then suppressed from ~Feb 25 (a week before HR drifts),
    rebounds during the recovery week, healthy through rebuild."""
    base = 64.0
    if d < date(2026, 2, 25):
        return base + RNG.uniform(-2, 2)
    if d < date(2026, 3, 18):
        t = (d - date(2026, 2, 25)).days / (date(2026, 3, 18) - date(2026, 2, 25)).days
        return _lerp(base, 49.0, t) + RNG.uniform(-1.5, 1.5)
    if d < date(2026, 3, 30):
        return 50.0 + RNG.uniform(-2, 2)
    if d < date(2026, 4, 13):
        t = (d - date(2026, 3, 30)).days / (date(2026, 4, 13) - date(2026, 3, 30)).days
        return _lerp(50.0, 67.0, t) + RNG.uniform(-1.5, 1.5)
    return 66.0 + RNG.uniform(-2, 2)


def _rhr(d: date) -> float:
    """bpm. Mirror image of HRV: creeps up during the overreach, back down after."""
    base = 48.0
    if d < date(2026, 3, 1):
        return base + RNG.uniform(-1, 1)
    if d < date(2026, 3, 22):
        t = (d - date(2026, 3, 1)).days / (date(2026, 3, 22) - date(2026, 3, 1)).days
        return _lerp(base, 56.0, t) + RNG.uniform(-1, 1)
    if d < date(2026, 4, 13):
        t = (d - date(2026, 3, 22)).days / (date(2026, 4, 13) - date(2026, 3, 22)).days
        return _lerp(56.0, 47.0, t) + RNG.uniform(-1, 1)
    return 46.0 + RNG.uniform(-1, 1)


def _sleep(d: date) -> float:
    """asleep hours. Dips through the overreach, recovers after."""
    ph = _phase(d)
    if ph == "overreach" and d >= date(2026, 2, 25):
        return RNG.uniform(6.0, 6.8)
    return RNG.uniform(7.1, 8.0)


# ---- weekly session template -------------------------------------------------
# Runs carry the HR-at-pace signal, so keep them on run-only days. Bike/swim on
# separate days add load without contaminating the run HR-at-pace proxy.
# weekday -> (sport, base_miles_or_minutes). Friday is rest.

def _activities_for(d: date, seq: int) -> list[Activity]:
    wd = d.weekday()  # Mon=0
    factor = _load_factor(d)
    out: list[Activity] = []

    def run(miles: float, label: str, hour: int) -> Activity:
        pace = _run_pace(d)
        hr = _run_hr(d) + RNG.uniform(-1.5, 1.5)
        mi = round(miles * factor, 2)
        secs = round(mi * pace * 60)
        return Activity(
            activity_id=f"T{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE,
            start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))),
            local_date=d, name=label, sport=Sport.RUN,
            moving_time_sec=float(secs), distance_mi=mi,
            avg_speed_mph=round(60.0 / pace, 2), avg_hr=round(hr, 1),
            max_hr=round(hr + RNG.uniform(8, 16), 1),
            avg_cadence=round(RNG.uniform(86, 92), 1),
            elevation_gain_ft=round(mi * RNG.uniform(30, 70), 1),
            suffer_score=round(secs / 60 * (hr / 150) * 1.1, 1),
        )

    def bike(minutes: float, label: str, hour: int) -> Activity:
        mins = minutes * factor
        hr = RNG.uniform(126, 134)
        speed = RNG.uniform(18, 21)
        mi = round(mins / 60 * speed, 2)
        watts = RNG.uniform(125, 145)
        return Activity(
            activity_id=f"T{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE,
            start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))),
            local_date=d, name=label, sport=Sport.RIDE,
            moving_time_sec=round(mins * 60), distance_mi=mi,
            avg_speed_mph=round(speed, 2), avg_hr=round(hr, 1),
            max_hr=round(hr + RNG.uniform(10, 20), 1), avg_watts=round(watts, 1),
            weighted_watts=round(watts + RNG.uniform(3, 12), 1),
            elevation_gain_ft=round(mi * RNG.uniform(20, 50), 1),
            suffer_score=round(mins * 0.8, 1),
        )

    def swim(minutes: float, label: str, hour: int) -> Activity:
        mins = minutes * factor
        mi = round(mins / 60 * RNG.uniform(1.6, 2.1), 2)
        hr = RNG.uniform(120, 132)
        return Activity(
            activity_id=f"T{seq:04d}", source=Source.STRAVA_API, athlete_id=ATHLETE,
            start_local=datetime.combine(d, time(hour, RNG.randint(0, 59))),
            local_date=d, name=label, sport=Sport.SWIM,
            moving_time_sec=round(mins * 60), distance_mi=mi,
            avg_hr=round(hr, 1), max_hr=round(hr + RNG.uniform(6, 14), 1),
            suffer_score=round(mins * 0.6, 1),
        )

    if wd == 0:      # Mon easy run
        out.append(run(4.0, "Morning Run", 6))
    elif wd == 1:    # Tue moderate run
        out.append(run(5.5, "Lunch Run", 12))
    elif wd == 2:    # Wed bike (no run)
        out.append(bike(50, "Afternoon Ride", 17))
    elif wd == 3:    # Thu run
        out.append(run(5.0, "Evening Run", 18))
    elif wd == 4:    # Fri rest
        pass
    elif wd == 5:    # Sat long run
        out.append(run(9.0, "Long Run", 8))
    elif wd == 6:    # Sun long bike, alternating with a short recovery swim
        out.append(bike(75, "Sunday Long Ride", 9))
        if seq % 2 == 0:
            out.append(swim(35, "Recovery Swim", 16))
    return out


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("synth_test.db")
    if db_path.exists():
        db_path.unlink()
    key = crypto.load_or_create_key(Path(".tokens/synth.key"))
    conn = db.connect(db_path)
    db.init_db(conn)

    activities: list[Activity] = []
    wellness: list[WellnessDay] = []
    seq = 1
    d = START
    while d <= END:
        for a in _activities_for(d, seq):
            activities.append(a)
            seq += 1
        wellness.append(WellnessDay(
            local_date=d, athlete_id=ATHLETE,
            asleep_hours=round(_sleep(d), 1),
            in_bed_hours=round(_sleep(d) + RNG.uniform(0.3, 0.8), 1),
            rhr=round(_rhr(d), 1), hrv=round(_hrv(d), 1),
            body_weight_lb=round(165 + RNG.uniform(-1.5, 1.5), 1),
        ))
        d += timedelta(days=1)

    db.upsert_activities(conn, activities, key=key)
    db.upsert_wellness(conn, wellness, key=key)
    conn.commit()
    print(f"wrote {len(activities)} activities + {len(wellness)} wellness days "
          f"-> {db_path}  ({START}..{END})")


if __name__ == "__main__":
    main()
