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
