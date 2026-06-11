"""Tests for build_daily_rows — one test per contract join rule."""

from datetime import date, datetime

from normalize.join import build_daily_rows
from schemas import Activity, Source, Sport, WellnessDay


def _act(i, sport, *, day="2026-06-01", time="07:00:00", dist=0.0, mins=60.0,
         avg_hr=None, max_hr=None, watts=None, wwatts=None, cadence=None,
         suffer=None, elev=None, source=Source.SHEET, athlete="ag"):
    start = datetime.fromisoformat(f"{day}T{time}")
    return Activity(
        activity_id=str(i), source=source, athlete_id=athlete,
        start_local=start, local_date=start.date(), name=f"a{i}",
        sport=sport, moving_time_sec=mins * 60, distance_mi=dist,
        avg_hr=avg_hr, max_hr=max_hr, avg_watts=watts, weighted_watts=wwatts,
        avg_cadence=cadence, suffer_score=suffer, elevation_gain_ft=elev,
    )


def _wellness(day="2026-06-01", athlete="ag", **over):
    base = dict(local_date=date.fromisoformat(day), athlete_id=athlete, rhr=47.0)
    base.update(over)
    return WellnessDay(**base)


def test_volume_fields_sum_across_a_multi_activity_day():
    rows = build_daily_rows([
        _act(1, Sport.RUN, time="07:00:00", dist=3.0, mins=30, elev=100),
        _act(2, Sport.RIDE, time="12:00:00", dist=20.0, mins=60, elev=500),
        _act(3, Sport.SWIM, time="18:00:00", dist=0.6, mins=25),
    ], [])
    assert len(rows) == 1
    r = rows[0]
    assert r.session_count == 3 and r.tri_session_count == 3
    assert r.run_miles == 3.0 and r.bike_miles == 20.0 and r.swim_miles == 0.6
    assert r.training_minutes == 115 and r.tri_training_minutes == 115
    assert r.elevation_gain_ft == 600
    assert r.activity_ids == ["1", "2", "3"]          # ordered by start time


def test_virtual_sports_count_toward_run_and_bike():
    r = build_daily_rows([
        _act(1, Sport.VIRTUAL_RUN, dist=2.0),
        _act(2, Sport.VIRTUAL_RIDE, time="12:00:00", dist=15.0),
    ], [])[0]
    assert r.run_miles == 2.0 and r.bike_miles == 15.0


def test_non_tri_sports_count_sessions_but_not_tri_fields():
    r = build_daily_rows([
        _act(1, Sport.WALK, dist=1.5, mins=40),
        _act(2, Sport.RUN, time="12:00:00", dist=3.0, mins=30),
    ], [])[0]
    assert r.session_count == 2 and r.tri_session_count == 1
    assert r.training_minutes == 70 and r.tri_training_minutes == 30
    assert r.run_miles == 3.0                          # walk miles don't leak in


def test_avg_hr_is_duration_weighted_and_skips_missing():
    r = build_daily_rows([
        _act(1, Sport.RUN, mins=30, avg_hr=100),
        _act(2, Sport.RIDE, time="12:00:00", mins=60, avg_hr=160),
        _act(3, Sport.SWIM, time="18:00:00", mins=45, avg_hr=None),  # excluded
    ], [])[0]
    assert r.avg_hr == (100 * 1800 + 160 * 3600) / 5400  # = 140


def test_power_scoped_to_bikes_and_cadence_to_runs():
    r = build_daily_rows([
        _act(1, Sport.RUN, mins=30, watts=300, cadence=88),   # run watts ignored
        _act(2, Sport.RIDE, time="12:00:00", mins=60, watts=180, wwatts=195, cadence=85),
    ], [])[0]
    assert r.avg_power_bike == 180
    assert r.weighted_power_bike == 195
    assert r.avg_cadence_run == 88                     # bike cadence ignored


def test_max_hr_is_max_and_pace_is_total_minutes_over_total_miles():
    r = build_daily_rows([
        _act(1, Sport.RUN, dist=2.0, mins=20, max_hr=165),
        _act(2, Sport.RUN, time="18:00:00", dist=4.0, mins=40, max_hr=178),
    ], [])[0]
    assert r.max_hr == 178
    assert r.avg_pace_run_min_per_mi == 10.0


def test_metric_fields_none_when_no_activity_has_them():
    r = build_daily_rows([_act(1, Sport.SWIM, dist=0.6, mins=25)], [])[0]
    assert r.avg_hr is None and r.max_hr is None
    assert r.avg_power_bike is None and r.avg_cadence_run is None
    assert r.avg_pace_run_min_per_mi is None           # no run miles -> no pace
    assert r.total_suffer_score is None


def test_suffer_score_sums_when_present():
    r = build_daily_rows([
        _act(1, Sport.RUN, suffer=40),
        _act(2, Sport.RIDE, time="12:00:00", suffer=55),
        _act(3, Sport.SWIM, time="18:00:00", suffer=None),
    ], [])[0]
    assert r.total_suffer_score == 95


def test_missing_wellness_never_drops_the_day():
    r = build_daily_rows([_act(1, Sport.RUN, dist=3.0)], [])[0]
    assert r.wellness is None
    assert r.session_count == 1


def test_wellness_only_rest_day_still_gets_a_row():
    rows = build_daily_rows([], [_wellness(day="2026-06-06")])
    assert len(rows) == 1
    r = rows[0]
    assert r.local_date == date(2026, 6, 6)
    assert r.session_count == 0 and r.training_minutes == 0
    assert r.source_mix == [] and r.activity_ids == []
    assert r.wellness.rhr == 47.0


def test_wellness_attaches_by_athlete_and_date():
    rows = build_daily_rows(
        [_act(1, Sport.RUN, dist=3.0)],
        [_wellness(), _wellness(athlete="basil", rhr=55.0)],
    )
    by_athlete = {r.athlete_id: r for r in rows}
    assert by_athlete["ag"].wellness.rhr == 47.0
    assert by_athlete["ag"].session_count == 1
    assert by_athlete["basil"].session_count == 0      # basil's day is wellness-only


def test_athletes_and_days_never_mix():
    rows = build_daily_rows([
        _act(1, Sport.RUN, dist=3.0, athlete="ag"),
        _act(2, Sport.RUN, dist=5.0, athlete="basil", source=Source.STRAVA_API),
        _act(3, Sport.RUN, day="2026-06-02", dist=1.0, athlete="ag"),
    ], [])
    assert len(rows) == 3
    ag_day1 = next(r for r in rows
                   if r.athlete_id == "ag" and r.local_date == date(2026, 6, 1))
    assert ag_day1.run_miles == 3.0
    assert ag_day1.source_mix == [Source.SHEET]


def test_source_mix_lists_distinct_sources():
    r = build_daily_rows([
        _act(1, Sport.RUN, source=Source.SHEET),
        _act(2, Sport.RIDE, time="12:00:00", source=Source.STRAVA_API),
        _act(3, Sport.SWIM, time="18:00:00", source=Source.SHEET),
    ], [])[0]
    assert sorted(s.value for s in r.source_mix) == ["sheet", "strava_api"]
