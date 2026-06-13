"""compute_metrics: rolling loads over a padded calendar series."""

from datetime import date, timedelta
from statistics import pstdev

from analyze.metrics import compute_metrics
from schemas import DailyRow, WellnessDay


def _day(d, minutes, *, athlete="ag", pace=None, hr=None, rhr=None, hrv=None):
    wellness = None
    if rhr is not None or hrv is not None:
        wellness = WellnessDay(
            local_date=date.fromisoformat(d), athlete_id=athlete, rhr=rhr, hrv=hrv
        )
    return DailyRow(
        local_date=date.fromisoformat(d), athlete_id=athlete, source_mix=[],
        session_count=1 if minutes else 0, tri_session_count=0,
        training_minutes=minutes, avg_pace_run_min_per_mi=pace, avg_hr=hr,
        wellness=wellness,
    )


def _streak(start="2026-05-01", days=28, minutes=60.0, **kw):
    d0 = date.fromisoformat(start)
    return [_day((d0 + timedelta(days=i)).isoformat(), minutes, **kw)
            for i in range(days)]


def test_acute_load_none_until_7_days_then_trailing_sum():
    ms = compute_metrics(_streak(days=8, minutes=60))
    assert [m.acute_load_7d for m in ms[:6]] == [None] * 6
    assert ms[6].acute_load_7d == 420            # 7 * 60
    assert ms[7].acute_load_7d == 420


def test_chronic_acwr_zscore_none_until_28_days():
    ms = compute_metrics(_streak(days=28, minutes=60))
    for m in ms[:27]:
        assert m.chronic_load_28d is None and m.acwr is None
        assert m.load_zscore_28d is None
    day28 = ms[27]
    assert day28.chronic_load_28d == 420         # mean 60 * 7
    assert day28.acwr == 1.0
    assert day28.load_zscore_28d is None         # constant load -> std 0


def test_zscore_matches_population_std():
    rows = _streak(days=27, minutes=60) + [_day("2026-05-28", 180.0)]
    m = compute_metrics(rows)[27]
    window = [60.0] * 27 + [180.0]
    mean = sum(window) / 28
    assert m.load_zscore_28d == (180.0 - mean) / pstdev(window)


def test_gap_days_count_as_zero_load_rest_days():
    rows = [_day("2026-06-01", 60.0), _day("2026-06-07", 60.0)]  # 5-day gap
    ms = compute_metrics(rows)
    assert len(ms) == 7                          # padded calendar series
    assert ms[3].rest_day is True and ms[3].acute_load_7d is None
    assert ms[6].acute_load_7d == 120            # 60 + five 0s + 60
    assert ms[0].rest_day is False and ms[6].rest_day is False


def test_acwr_none_when_chronic_is_zero():
    ms = compute_metrics(_streak(days=28, minutes=0.0))
    assert ms[27].chronic_load_28d == 0
    assert ms[27].acwr is None


def test_athletes_are_windowed_independently():
    rows = _streak(days=7, minutes=60, athlete="ag") + \
           _streak(days=7, minutes=30, athlete="basil")
    ms = compute_metrics(rows)
    by = {(m.athlete_id, m.local_date): m for m in ms}
    assert by[("ag", date(2026, 5, 7))].acute_load_7d == 420
    assert by[("basil", date(2026, 5, 7))].acute_load_7d == 210


def test_pace_trend_recent_7d_vs_prior_7d_positive_means_slowing():
    rows = _streak(days=7, minutes=60, pace=10.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=11.0)
    ms = compute_metrics(rows)
    assert ms[12].pace_trend_pct_14d is None     # window incomplete
    assert ms[13].pace_trend_pct_14d == 10.0     # (11 - 10) / 10 * 100


def test_pace_trend_none_when_a_half_has_too_few_run_days():
    # prior half has a single paced day (< MIN_TREND_DAYS_PER_HALF)
    rows = [_day("2026-05-01", 60, pace=10.0)] + \
           _streak(start="2026-05-02", days=6, minutes=60) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=11.0)
    assert compute_metrics(rows)[13].pace_trend_pct_14d is None


def test_hr_at_pace_trend_uses_beats_per_mile():
    # beats/mi: prior 140*10=1400, recent 147*10=1470 -> +5%
    rows = _streak(days=7, minutes=60, pace=10.0, hr=140.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=10.0, hr=147.0)
    m = compute_metrics(rows)[13]
    assert round(m.hr_at_pace_trend_pct_14d, 6) == 5.0
    assert m.pace_trend_pct_14d == 0.0           # pace itself is steady


def test_hr_at_pace_needs_both_hr_and_pace():
    rows = _streak(days=7, minutes=60, pace=10.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=10.0, hr=150.0)
    assert compute_metrics(rows)[13].hr_at_pace_trend_pct_14d is None
