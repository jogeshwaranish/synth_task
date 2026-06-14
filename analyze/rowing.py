"""Rowing-erg performance anomalies. Owner: Anish (data-pipeline seam).

Additive sibling of analyze/metrics.py — deliberately NOT folded into Basil's
detectors (his module is the LOCKED metrics surface; this is a new seam, flagged
for his review like the `# TODO(security)` seams). It emits standard `Anomaly`
rows, whose `metric` field is a free string per the contract, so no schema bump.

Why a separate detector: the daily training-load metrics are sport-agnostic and
already work for erg volume, but the rowing-specific signal — the per-500m SPLIT
trend — is invisible to them (pace_trend is run min/mile). And split is not
comparable across piece types (a 2k max effort ~1:45/500m vs a 6k threshold
~1:58/500m), so we trend WITHIN each piece family, never pooled.

Input is the erg Activities produced by ingest/rowing.py: split rides in
avg_speed_mph (we invert it back to sec/500m) and the piece label is the name
prefix ("2x6k erg @1:59.3/500m").
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from ingest.rowing import _MPS_PER_MPH
from schemas import Activity, Anomaly, AnomalySeverity, Source, Sport

# --- tunable thresholds (DECISIONS.md) --------------------------------------
MIN_SESSIONS_PER_PIECE = 3      # too few comparable tests -> no judgement
ERG_REG_WATCH_PCT = 1.5         # a session this far off the best-to-date split
ERG_REG_FLAG_PCT = 3.0
ERG_PLATEAU_DAYS = 21           # no PR in this long (despite testing) = plateau


def _split_sec(act: Activity) -> float | None:
    if not act.avg_speed_mph or act.avg_speed_mph <= 0:
        return None
    return 500.0 * _MPS_PER_MPH / act.avg_speed_mph


def _piece(act: Activity) -> str:
    return re.split(r"\s+erg\b", act.name, maxsplit=1)[0].strip().lower()


def _clock(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def _erg_sessions(activities: list[Activity]) -> list[tuple[str, str, date, float]]:
    """(athlete_id, piece, date, split_sec) for every usable erg Activity."""
    out = []
    for a in activities:
        if a.source is not Source.SHEET or a.sport is not Sport.OTHER:
            continue
        if " erg" not in a.name.lower():
            continue
        s = _split_sec(a)
        if s is not None:
            out.append((a.athlete_id, _piece(a), a.local_date, s))
    return out


def _anomaly(athlete_id: str, d: date, metric: str, value: float,
             sev: AnomalySeverity, desc: str, *, baseline: float | None = None) -> Anomaly:
    # Same deterministic-id scheme as analyze/metrics so re-runs upsert in place.
    return Anomaly(anomaly_id=f"{athlete_id}:{d.isoformat()}:{metric}", local_date=d,
                   metric=metric, value=round(value, 2), baseline=baseline,
                   severity=sev, description=desc)


def detect_erg_anomalies(activities: list[Activity]) -> list[Anomaly]:
    by_group: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    for athlete_id, piece, d, split in _erg_sessions(activities):
        by_group[(athlete_id, piece)].append((d, split))

    out: list[Anomaly] = []
    for (athlete_id, piece), sessions in by_group.items():
        if len(sessions) < MIN_SESSIONS_PER_PIECE:
            continue
        sessions.sort()
        best, best_date = sessions[0][1], sessions[0][0]
        for i, (d, split) in enumerate(sessions):
            if i > 0 and split > best:                       # off the best-to-date
                pct = (split - best) / best * 100
                if pct >= ERG_REG_WATCH_PCT:
                    sev = (AnomalySeverity.FLAG if pct >= ERG_REG_FLAG_PCT
                           else AnomalySeverity.WATCH)
                    out.append(_anomaly(
                        athlete_id, d, "erg_split_regression", pct, sev,
                        f"{piece} split {_clock(split)}/500m is {pct:+.1f}% off the "
                        f"season-best {_clock(best)} (set {best_date.isoformat()}).",
                        baseline=round(best, 1)))
            if split < best:
                best, best_date = split, d

        # Plateau: the latest test set no PR and the best is stale -> adaptation
        # has stalled despite continued testing.
        last_date, last_split = sessions[-1]
        if last_split > best and (last_date - best_date).days >= ERG_PLATEAU_DAYS:
            out.append(_anomaly(
                athlete_id, last_date, "erg_split_plateau",
                (last_date - best_date).days, AnomalySeverity.WATCH,
                f"No {piece} PR in {(last_date - best_date).days} days: latest "
                f"{_clock(last_split)}/500m vs best {_clock(best)} on "
                f"{best_date.isoformat()} — erg gains have flattened.",
                baseline=round(best, 1)))
    return out
