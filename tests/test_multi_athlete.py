"""Offline tests for the multi-athlete harness (no workbook, no network).

Synthetic erg Activities stand in for the real sheet; a fake `llm` stands in for
Anthropic. Covers: roster enumeration, trend classification, pattern validation,
deterministic simulation, and the end-to-end build producing DIFFERENT outcomes
for a clean improver vs a planted overreacher.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

from ingest.rowing import RowingRoster, _MPS_PER_MPH
from schemas import Activity, Source, Sport
from scripts.multi_athlete import (
    MultiAthleteHarness, PatternConfig, PatternRejected, classify_trend,
    default_pattern, plan_pattern, simulate, validate_pattern,
)
from store import db

# 7 weekly-ish 2x6k test dates, like the real workbook's spread.
_DATES = [date(2026, 1, 5) + timedelta(days=14 * i) for i in range(7)]


def _split_to_mph(split_sec: float) -> float:
    return (500.0 / split_sec) * _MPS_PER_MPH


def _erg(athlete_id: str, splits_sec: list[float], piece: str = "2x6k") -> list[Activity]:
    out = []
    for d, sp in zip(_DATES, splits_sec):
        out.append(Activity(
            activity_id=f"erg-{athlete_id}-{d.isoformat()}-{piece}",
            source=Source.SHEET, athlete_id=athlete_id,
            start_local=datetime.combine(d, time(17, 0)), local_date=d,
            name=f"{piece} erg @2:00.0/500m", sport=Sport.OTHER,
            moving_time_sec=2160.0, distance_mi=7.456,
            avg_speed_mph=round(_split_to_mph(sp), 3)))
    return out


# improver: splits fall steadily, last == season best
_IMPROVER = _erg("imp", [125, 124, 123, 122, 121, 120.5, 120])
# plateau: essentially flat
_PLATEAU = _erg("plt", [123, 123.2, 122.8, 123.1, 123, 122.9, 123.1])
# overreacher: improves then regresses hard off the best
_OVERREACH = _erg("ovr", [123, 121, 119, 118, 120, 123, 126])


# --------------------------------------------------------------------------
# roster enumeration
# --------------------------------------------------------------------------

def test_all_athletes_lists_unique_sorted_ids():
    roster = RowingRoster.from_rows(
        [{"L": "Banks", "F": "Claire"}, {"L": "Cox", "F": "Madeline"},
         {"L": "Banks", "F": "Claire"}],
        last_col="L", first_col="F")
    assert roster.all_athletes() == ["banks-claire", "cox-madeline"]


# --------------------------------------------------------------------------
# trend classification
# --------------------------------------------------------------------------

def test_classify_improver_is_adapting():
    t = classify_trend(_IMPROVER)
    assert t is not None and t.category == "adapting"
    assert t.piece == "2x6k" and t.n_tests == 7
    assert t.improvement_sec == pytest.approx(5.0, abs=0.05)  # mph round-trip


def test_classify_flat_is_plateau():
    assert classify_trend(_PLATEAU).category == "plateau"


def test_classify_regression_is_overreaching():
    assert classify_trend(_OVERREACH).category == "overreaching"


def test_classify_returns_none_without_enough_tests():
    assert classify_trend(_erg("x", [120, 121])) is None


# --------------------------------------------------------------------------
# pattern validation (reject bad LLM output)
# --------------------------------------------------------------------------

def _good_overreach_cfg() -> dict:
    return {
        "window_start": "2026-01-01", "window_end": "2026-03-18",
        "baseline_load": 1.0, "overload_start": "2026-02-10",
        "overload_peak_factor": 1.6, "hrv_drop": 16, "rhr_rise": 9, "sleep_dip": 1.4,
    }


def test_validate_accepts_in_bounds_config():
    t = classify_trend(_OVERREACH)
    cfg = validate_pattern(_good_overreach_cfg(), t)
    assert cfg.overload_start == date(2026, 2, 10) and cfg.overload_peak_factor == 1.6


def test_validate_rejects_non_lean_window():
    t = classify_trend(_OVERREACH)
    bad = _good_overreach_cfg() | {"window_start": "2025-06-01"}  # >98d span
    with pytest.raises(PatternRejected):
        validate_pattern(bad, t)


def test_validate_rejects_overload_for_adapting():
    t = classify_trend(_IMPROVER)
    with pytest.raises(PatternRejected):
        validate_pattern(_good_overreach_cfg(), t)  # adapting must have no overload


def test_validate_rejects_out_of_cap_markers():
    t = classify_trend(_OVERREACH)
    with pytest.raises(PatternRejected):
        validate_pattern(_good_overreach_cfg() | {"hrv_drop": 99}, t)


def test_plan_pattern_falls_back_on_garbage_llm():
    t = classify_trend(_OVERREACH)
    cfg = plan_pattern(t, llm=lambda _p: "not json at all")
    assert cfg == default_pattern(t)  # rejected -> deterministic fallback


# --------------------------------------------------------------------------
# deterministic simulation + planted effect
# --------------------------------------------------------------------------

def test_simulate_is_deterministic():
    t = classify_trend(_OVERREACH)
    cfg = default_pattern(t)
    a1, w1 = simulate("ovr", cfg)
    a2, w2 = simulate("ovr", cfg)
    assert [a.model_dump() for a in a1] == [a.model_dump() for a in a2]
    assert [w.model_dump() for w in w1] == [w.model_dump() for w in w2]


def _late_mean_hrv(wellness, cfg: PatternConfig) -> float:
    tail = [w.hrv for w in wellness if w.local_date >= cfg.window_end - timedelta(days=14)]
    return sum(tail) / len(tail)


def test_overreach_sim_suppresses_late_hrv_vs_adapting():
    over = default_pattern(classify_trend(_OVERREACH))
    adapt = default_pattern(classify_trend(_IMPROVER))
    _, w_over = simulate("ovr", over)
    _, w_adapt = simulate("imp", adapt)
    assert _late_mean_hrv(w_over, over) < _late_mean_hrv(w_adapt, adapt) - 5


# --------------------------------------------------------------------------
# end-to-end build: different athletes -> different outcomes
# --------------------------------------------------------------------------

def test_build_flags_overreacher_not_improver(tmp_path):
    erg = {"ovr": _erg("ovr", [123, 121, 119, 118, 120, 123, 126]),
           "imp": _erg("imp", [125, 124, 123, 122, 121, 120.5, 120])}
    harness = MultiAthleteHarness(["ovr", "imp"], lambda cid: erg[cid], llm=None)
    db_path = tmp_path / "two.db"
    results = harness.build(db_path)

    by_id = {r.athlete_id: r for r in results}
    assert by_id["ovr"].category == "overreaching"
    assert by_id["imp"].category == "adapting"
    # the planted overreacher accumulates anomalies; the clean improver far fewer
    assert by_id["ovr"].n_anomalies > by_id["imp"].n_anomalies

    # stored DB is plaintext + queryable without a key
    conn = db.connect(db_path)
    acts = db.get_activities(conn)
    assert {a.athlete_id for a in acts} == {"ovr", "imp"}
    # erg names survive in cleartext (key=None) -> committed artifact is portable
    assert any("erg" in a.name for a in acts)


def test_build_with_fake_llm_uses_validated_config(tmp_path):
    erg = {"ovr": _erg("ovr", [123, 121, 119, 118, 120, 123, 126])}

    def fake_llm(_prompt: str) -> str:
        return json.dumps(_good_overreach_cfg())

    harness = MultiAthleteHarness(["ovr"], lambda cid: erg[cid], llm=fake_llm)
    results = harness.build(tmp_path / "one.db")
    assert results[0].category == "overreaching" and results[0].n_sim_activities > 0
