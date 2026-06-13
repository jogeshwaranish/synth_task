"""detect_anomalies: each detector fires at its threshold, silent below it."""

from datetime import date

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
