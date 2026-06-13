"""detect_anomalies: each detector fires at its threshold, silent below it."""

from datetime import date, timedelta

from analyze.metrics import detect_anomalies
from schemas import AnomalySeverity, DailyMetrics, DailyRow, WellnessDay


def _metric(d="2026-06-01", *, athlete="ag", **over):
    base = dict(local_date=date.fromisoformat(d), athlete_id=athlete)
    base.update(over)
    return DailyMetrics(**base)


def _row(d="2026-06-01", minutes=60.0, *, athlete="ag", rhr=None, hrv=None):
    wellness = None
    if rhr is not None or hrv is not None:
        wellness = WellnessDay(
            local_date=date.fromisoformat(d), athlete_id=athlete, rhr=rhr, hrv=hrv
        )
    return DailyRow(
        local_date=date.fromisoformat(d), athlete_id=athlete, source_mix=[],
        session_count=1, tri_session_count=0, training_minutes=minutes,
        wellness=wellness,
    )


def _only(anomalies, metric):
    return [a for a in anomalies if a.metric == metric]


def test_acwr_flag_above_1_5_watch_outside_safe_window_silent_inside():
    cases = {1.6: AnomalySeverity.FLAG, 1.4: AnomalySeverity.WATCH,
             0.7: AnomalySeverity.WATCH}
    for value, expected in cases.items():
        ms = [_metric(acwr=value, acute_load_7d=420.0, chronic_load_28d=300.0)]
        (a,) = _only(detect_anomalies([], ms), "acwr")
        assert a.severity == expected and a.value == value
    for value in (0.8, 1.0, 1.3):
        ms = [_metric(acwr=value, acute_load_7d=420.0, chronic_load_28d=420.0)]
        assert _only(detect_anomalies([], ms), "acwr") == []
    assert detect_anomalies([], [_metric()]) == []   # acwr None -> silent


def test_load_zscore_fires_high_side_only():
    rows = [_row(minutes=240.0)]
    flag = _metric(load_zscore_28d=3.5, chronic_load_28d=420.0)
    watch = _metric(load_zscore_28d=2.5, chronic_load_28d=420.0)
    (a,) = _only(detect_anomalies(rows, [flag]), "load_zscore_28d")
    assert a.severity == AnomalySeverity.FLAG
    assert a.value == 240.0                      # the day's training_minutes
    assert a.baseline == 60.0                    # chronic / 7 = 28d daily mean
    assert a.zscore == 3.5
    (a,) = _only(detect_anomalies(rows, [watch]), "load_zscore_28d")
    assert a.severity == AnomalySeverity.WATCH
    for z in (1.9, -2.5, -3.5, None):            # low side is ACWR's job
        assert _only(detect_anomalies(rows, [_metric(load_zscore_28d=z,
                     chronic_load_28d=420.0)]), "load_zscore_28d") == []


def test_anomaly_ids_are_deterministic():
    ms = [_metric(acwr=1.6, acute_load_7d=400.0, chronic_load_28d=250.0)]
    (a,) = detect_anomalies([], ms)
    (b,) = detect_anomalies([], ms)
    assert a.anomaly_id == b.anomaly_id == "ag:2026-06-01:acwr"


def test_descriptions_are_code_authored_and_carry_the_numbers():
    ms = [_metric(acwr=1.62, acute_load_7d=420.0, chronic_load_28d=259.0)]
    (a,) = detect_anomalies([], ms)
    assert "1.62" in a.description and "0.8" in a.description


def test_trend_detectors_watch_over_5pct_flag_over_10pct():
    for metric in ("pace_trend_pct_14d", "hr_at_pace_trend_pct_14d"):
        for value, expected in ((12.0, AnomalySeverity.FLAG),
                                (6.0, AnomalySeverity.WATCH)):
            (a,) = _only(detect_anomalies([], [_metric(**{metric: value})]), metric)
            assert a.severity == expected and a.value == value
        for value in (4.0, -6.0, -12.0, None):   # improving/steady = silent
            assert _only(detect_anomalies([], [_metric(**{metric: value})]),
                         metric) == []


def _wellness_history(values, day_of_interest_value, *, field="rhr"):
    """14 baseline days then the day under test (2026-06-15)."""
    d0 = date.fromisoformat("2026-06-01")
    rows = [_row((d0 + timedelta(days=i)).isoformat(), **{field: v})
            for i, v in enumerate(values)]
    rows.append(_row("2026-06-15", **{field: day_of_interest_value}))
    return rows


def test_rhr_elevated_vs_28d_baseline():
    baseline = [46.0, 48.0] * 7                  # mean 47, population std 1
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 50.0), []), "rhr")
    assert a.severity == AnomalySeverity.FLAG    # z = +3
    assert a.value == 50.0 and a.baseline == 47.0 and a.zscore == 3.0
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 49.5), []), "rhr")
    assert a.severity == AnomalySeverity.WATCH   # z = +2.5
    assert _only(detect_anomalies(_wellness_history(baseline, 48.0), []), "rhr") == []
    # LOW rhr is good news, never an anomaly
    assert _only(detect_anomalies(_wellness_history(baseline, 44.0), []), "rhr") == []


def test_hrv_suppressed_vs_28d_baseline():
    baseline = [60.0, 80.0] * 7                  # mean 70, population std 10
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 40.0, field="hrv"), []),
                 "hrv")
    assert a.severity == AnomalySeverity.FLAG    # z = -3
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 45.0, field="hrv"), []),
                 "hrv")
    assert a.severity == AnomalySeverity.WATCH   # z = -2.5
    # HIGH hrv is good news, never an anomaly
    assert _only(detect_anomalies(_wellness_history(baseline, 100.0, field="hrv"), []),
                 "hrv") == []


def test_wellness_detectors_gate_on_baseline_days_and_variance():
    too_few = [46.0, 48.0] * 6                   # 12 < MIN_WELLNESS_BASELINE_DAYS
    assert _only(detect_anomalies(_wellness_history(too_few, 60.0), []), "rhr") == []
    flat = [47.0] * 14                           # std 0
    assert _only(detect_anomalies(_wellness_history(flat, 60.0), []), "rhr") == []
