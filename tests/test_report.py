"""resolve_target + generate_report — offline, fake Anthropic client."""

from datetime import date

import pytest

from schemas import DailyMetrics
from store import db
from synthesize.report import resolve_target


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def _metric(d, athlete):
    return DailyMetrics(local_date=date.fromisoformat(d), athlete_id=athlete,
                        rest_day=False)


def test_resolve_target_defaults_to_busiest_athlete_and_full_span(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [
        _metric("2026-06-01", "ag"), _metric("2026-06-05", "ag"),
        _metric("2026-06-03", "ag"), _metric("2026-06-02", "basil"),
    ])
    athlete, start, end = resolve_target(conn, None, None, None)
    assert athlete == "ag"                          # most metrics rows
    assert start == date(2026, 6, 1) and end == date(2026, 6, 5)


def test_resolve_target_honors_explicit_overrides(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [_metric("2026-06-01", "ag"),
                             _metric("2026-06-02", "basil")])
    athlete, start, end = resolve_target(conn, "basil", "2026-06-10", "2026-06-20")
    assert athlete == "basil"
    assert start == date(2026, 6, 10) and end == date(2026, 6, 20)


def test_resolve_target_raises_when_no_metrics(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="run analyze"):
        resolve_target(conn, None, None, None)


from datetime import datetime

from config import Settings
from schemas import Activity, Anomaly, AnomalySeverity, Source, Sport
from security import crypto
from synthesize.report import generate_report
from tests.test_agent_loop import FakeClient, _report_json, _text
from types import SimpleNamespace


def _settings(tmp_path):
    return Settings(_env_file=None, anthropic_api_key="k",
                    synth_token_dir=tmp_path / "tok",
                    synth_db_path=tmp_path / "synth.db")


def _seed_full(conn, key):
    start = datetime.fromisoformat("2026-06-03T07:00:00")
    db.upsert_activities(conn, [Activity(
        activity_id="a1", source=Source.SHEET, athlete_id="ag",
        start_local=start, local_date=start.date(), name="Hard intervals",
        sport=Sport.RUN, moving_time_sec=3600, distance_mi=8.0,
    )], key=key)
    db.upsert_metrics(conn, [_metric("2026-06-01", "ag"), _metric("2026-06-07", "ag")])
    db.upsert_anomalies(conn, [Anomaly(
        anomaly_id="ag:2026-06-03:acwr", local_date=date(2026, 6, 3),
        metric="acwr", value=1.6, baseline=1.0, zscore=None,
        severity=AnomalySeverity.FLAG, description="ACWR 1.60 above safe window.",
    )])


def test_generate_report_resolves_target_and_returns_validated_report(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed_full(conn, key)

    client = FakeClient([
        SimpleNamespace(stop_reason="end_turn", content=[_text(_report_json())]),
    ])
    report = generate_report(conn, s, client=client)
    assert report.athlete_id == "ag"
    assert report.report_id and report.contract_version == "1.0"
    # Period was resolved from the stored metrics span and passed through.
    assert report.data_coverage["n_activities"] == 1
