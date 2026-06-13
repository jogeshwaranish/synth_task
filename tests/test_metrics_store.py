"""Round-trip + idempotency for the daily_metrics and anomaly tables."""

from datetime import date

from schemas import Anomaly, AnomalySeverity, DailyMetrics
from store import db


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def _metrics(d="2026-06-01", athlete="ag", **over):
    base = dict(
        local_date=date.fromisoformat(d), athlete_id=athlete,
        acute_load_7d=420.0, chronic_load_28d=300.0, acwr=1.4,
        load_zscore_28d=1.1, pace_trend_pct_14d=2.0,
        hr_at_pace_trend_pct_14d=-1.0, rest_day=False,
    )
    base.update(over)
    return DailyMetrics(**base)


def _anomaly(metric="acwr", d="2026-06-01", athlete="ag"):
    return Anomaly(
        anomaly_id=f"{athlete}:{d}:{metric}",
        local_date=date.fromisoformat(d), metric=metric, value=1.4,
        baseline=1.0, zscore=None, severity=AnomalySeverity.WATCH,
        description="ACWR 1.40 is above the safe window 0.8-1.3.",
    )


def test_metrics_roundtrip_and_idempotent_upsert(tmp_path):
    conn = _conn(tmp_path)
    m = _metrics()
    assert db.upsert_metrics(conn, [m, _metrics(d="2026-06-02", rest_day=True)]) == 2
    db.upsert_metrics(conn, [m])                 # same (athlete, date) again
    got = db.get_metrics(conn)
    assert len(got) == 2 and got[0] == m
    assert got[1].rest_day is True


def test_metrics_filtered_by_athlete(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [_metrics(), _metrics(athlete="basil", acwr=None)])
    got = db.get_metrics(conn, athlete_id="basil")
    assert [m.athlete_id for m in got] == ["basil"]
    assert got[0].acwr is None


def test_anomaly_roundtrip_and_idempotent_on_id(tmp_path):
    conn = _conn(tmp_path)
    a = _anomaly()
    assert db.upsert_anomalies(conn, [a]) == 1
    db.upsert_anomalies(conn, [a])               # same anomaly_id -> upsert
    got = db.get_anomalies(conn)
    assert got == [a]
    assert got[0].severity is AnomalySeverity.WATCH
