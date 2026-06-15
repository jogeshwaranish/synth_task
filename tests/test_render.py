"""The human-readable Markdown renderer for a SynthesisReport. Offline."""

from __future__ import annotations

from datetime import date, datetime, timezone

from schemas import Evidence, Pattern, SynthesisReport
from synthesize.render import render_markdown


def _report() -> SynthesisReport:
    return SynthesisReport(
        report_id="r1", generated_at=datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc),
        athlete_id="banks_claire", period_start=date(2025, 9, 8),
        period_end=date(2026, 3, 16),
        data_coverage={"n_days": 190, "n_activities": 206, "n_wellness_days": 190},
        summary="Claire is under-recovered, not unfit.",
        patterns=[Pattern(
            pattern_id="p1", title="Fatigue is stacking up",
            description="Take a down week. EVIDENCE: ACWR sat 1.1-1.2 through March.",
            kind="trend", date_start=date(2026, 1, 13), date_end=date(2026, 3, 16),
            metrics_involved=["acwr", "chronic_load_28d"],
            confidence="high", caveats="ACWR stayed moderate, not extreme.")],
        anomalies_reviewed=["banks_claire:2026-03-11:erg_split_plateau"],
        open_questions=["Is this a planned race build?"],
        evidence=[Evidence(step=1, tool="query_anomalies", args={},
                           result_digest="query_anomalies -> 36 rows")],
    )


def test_render_has_sections_and_humanises():
    md = render_markdown(_report())
    assert "# Training Insights — banks_claire" in md
    assert "## The big picture" in md
    assert "Claire is under-recovered" in md
    assert "### 1. Fatigue is stacking up" in md
    # takeaway and evidence are split apart
    assert "Take a down week." in md
    assert "**Evidence:** ACWR sat 1.1-1.2 through March." in md
    assert "**Worth noting:** ACWR stayed moderate" in md
    assert "## Questions to follow up with the athlete" in md
    assert "Is this a planned race build?" in md
    # audit trail is rendered with the reviewed count
    assert "reviewed **1**" in md
    assert "`query_anomalies`" in md
    assert "`acwr`" in md


def test_render_handles_no_patterns():
    r = _report()
    r.patterns = []
    md = render_markdown(r)
    assert "_No patterns surfaced for this period._" in md
