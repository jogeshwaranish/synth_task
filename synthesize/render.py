"""Render a validated SynthesisReport as a human-readable Markdown briefing.

AG (and any non-technical reader) shouldn't have to parse JSON. This turns the
contract object the agent produced into a coach-style writeup: the big picture,
the few patterns that matter (takeaway split from its supporting evidence), the
questions to ask the athlete, and the audit trail of what the system actually
looked at. Pure formatting — no I/O, no model calls; it consumes the same
validated report the JSON deliverable is built from.
"""

from __future__ import annotations

from schemas import Evidence, Pattern, SynthesisReport

_KIND_LABEL = {
    "trend": "Trend",
    "correlation": "Connection between signals",
    "anomaly_explanation": "Explanation",
    "observation": "Observation",
}
_CONFIDENCE_LABEL = {"high": "high confidence",
                     "medium": "medium confidence",
                     "low": "low confidence"}


def _split_evidence(description: str) -> tuple[str, str | None]:
    """Patterns end with 'EVIDENCE: ...'; separate it so the plain-language
    takeaway reads first and the numbers sit underneath."""
    for marker in ("EVIDENCE:", "Evidence:"):
        if marker in description:
            takeaway, ev = description.split(marker, 1)
            return takeaway.strip(), ev.strip()
    return description.strip(), None


def _render_pattern(i: int, p: Pattern) -> list[str]:
    takeaway, evidence = _split_evidence(p.description)
    kind = _KIND_LABEL.get(p.kind, p.kind)
    out = [
        f"### {i}. {p.title}",
        f"*{kind} · {_CONFIDENCE_LABEL.get(p.confidence.value, p.confidence.value)} "
        f"· {p.date_start.isoformat()} → {p.date_end.isoformat()}*",
        "",
        takeaway,
    ]
    if evidence:
        out += ["", f"> **Evidence:** {evidence}"]
    if p.caveats:
        out += ["", f"> **Worth noting:** {p.caveats}"]
    if p.metrics_involved:
        out += ["", "_Metrics: " + ", ".join(f"`{m}`" for m in p.metrics_involved) + "_"]
    out.append("")
    return out


def _render_evidence_step(e: Evidence) -> str:
    args = ", ".join(f"{k}={v}" for k, v in e.args.items()) or "—"
    return f"{e.step}. `{e.tool}`({args}) → {e.result_digest}"


def render_markdown(report: SynthesisReport) -> str:
    cov = report.data_coverage or {}
    coverage = (
        f"{cov.get('n_activities', '?')} activities · "
        f"{cov.get('n_wellness_days', '?')} wellness days · "
        f"{cov.get('n_days', '?')} days covered"
    )
    lines: list[str] = [
        f"# Training Insights — {report.athlete_id}",
        f"*{report.period_start.isoformat()} → {report.period_end.isoformat()} · "
        f"{coverage}*",
        f"*Generated {report.generated_at:%Y-%m-%d %H:%M UTC}*",
        "",
        "## The big picture",
        "",
        report.summary,
        "",
        "## What stands out",
        "",
    ]
    if report.patterns:
        for i, p in enumerate(report.patterns, 1):
            lines += _render_pattern(i, p)
    else:
        lines += ["_No patterns surfaced for this period._", ""]

    if report.open_questions:
        lines += ["## Questions to follow up with the athlete", ""]
        lines += [f"- {q}" for q in report.open_questions]
        lines += [""]

    lines += [
        "## How this was checked",
        "",
        f"The system reviewed **{len(report.anomalies_reviewed)}** flagged data "
        f"points and ran the following queries to reach the conclusions above "
        f"(this trail is recorded by the system, not the AI):",
        "",
    ]
    if report.evidence:
        lines += [_render_evidence_step(e) for e in report.evidence]
    else:
        lines += ["_(no tool calls recorded)_"]
    lines += [""]
    return "\n".join(lines)
