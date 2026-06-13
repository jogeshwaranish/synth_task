"""Report generation seam shared by the CLI and the FastAPI layer. Owner: Basil.

Resolves which athlete + date window to report on (from what's actually in the
store, so the no-flags case works against whatever was ingested), then drives
the synthesis agent. Thin: all heavy lifting lives in analyze/, store/, and
synthesize/agent.py.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from config import Settings
from schemas import SynthesisReport
from security import crypto
from store import db
from synthesize.agent import run_synthesis


def resolve_target(
    conn, athlete: str | None, start: str | None, end: str | None
) -> tuple[str, date, date]:
    metrics = db.get_metrics(conn)
    if not metrics:
        raise ValueError("no daily metrics in the store — run analyze first")

    if athlete is None:
        counts = Counter(m.athlete_id for m in metrics)
        athlete = counts.most_common(1)[0][0]

    dates = [m.local_date for m in metrics if m.athlete_id == athlete]
    if not dates:
        raise ValueError(f"no daily metrics for athlete '{athlete}'")

    period_start = date.fromisoformat(start) if start else min(dates)
    period_end = date.fromisoformat(end) if end else max(dates)
    return athlete, period_start, period_end


def generate_report(
    conn, settings: Settings, *,
    athlete: str | None = None, start: str | None = None, end: str | None = None,
    key: bytes | None = None, client=None,
) -> SynthesisReport:
    if key is None:
        key = crypto.load_or_create_key(settings.encryption_key_path)
    athlete_id, period_start, period_end = resolve_target(conn, athlete, start, end)
    return run_synthesis(conn, settings, athlete_id, period_start, period_end,
                         key=key, client=client)
