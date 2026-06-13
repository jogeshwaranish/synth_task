"""The four agent tool backends — deterministic, offline, over a seeded store."""

from datetime import date, datetime

from schemas import (
    Activity, Anomaly, AnomalySeverity, DailyMetrics, Source, Sport, SwimSplit,
)
from security import crypto
from store import db
from synthesize import tools


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def _anomaly(metric, sev, d="2026-06-01"):
    return Anomaly(
        anomaly_id=f"ag:{d}:{metric}", local_date=date.fromisoformat(d),
        metric=metric, value=1.6, baseline=1.0, zscore=None, severity=sev,
        description=f"{metric} fired",
    )


def test_tool_schemas_cover_the_four_contract_tools():
    names = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert names == {"get_daily_metrics", "get_activity_detail",
                     "compare_periods", "query_anomalies"}
    assert names == set(tools.TOOL_NAMES)
    for t in tools.TOOL_SCHEMAS:                 # Anthropic tool shape
        assert t["input_schema"]["type"] == "object"


def test_query_anomalies_lists_all_then_filters_by_severity(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_anomalies(conn, [
        _anomaly("acwr", AnomalySeverity.FLAG),
        _anomaly("rhr", AnomalySeverity.WATCH),
    ])
    everything = tools.query_anomalies(conn)
    assert {a["metric"] for a in everything} == {"acwr", "rhr"}
    assert everything[0]["description"] == "acwr fired"   # trusted text, not wrapped
    only_flag = tools.query_anomalies(conn, severity="flag")
    assert [a["metric"] for a in only_flag] == ["acwr"]


def _metric(d, athlete="ag", **over):
    base = dict(local_date=date.fromisoformat(d), athlete_id=athlete,
                acute_load_7d=420.0, acwr=1.1, rest_day=False)
    base.update(over)
    return DailyMetrics(**base)


def test_get_daily_metrics_filters_to_inclusive_date_range(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [
        _metric("2026-05-31"), _metric("2026-06-01"), _metric("2026-06-02"),
        _metric("2026-06-03"),
    ])
    got = tools.get_daily_metrics(
        conn, athlete_id="ag", date_start="2026-06-01", date_end="2026-06-02"
    )
    assert [m["local_date"] for m in got] == ["2026-06-01", "2026-06-02"]
    assert got[0]["acute_load_7d"] == 420.0


def test_get_daily_metrics_scopes_to_athlete(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [_metric("2026-06-01", athlete="ag"),
                             _metric("2026-06-01", athlete="basil")])
    got = tools.get_daily_metrics(
        conn, athlete_id="basil", date_start="2026-06-01", date_end="2026-06-01"
    )
    assert [m["athlete_id"] for m in got] == ["basil"]
