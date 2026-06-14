"""Build ONE combined test DB covering every athlete in AG's rowing workbook.

For each rostered athlete: ingest their real erg sessions via the AI-fallback
ingest, plant a lean simulated-Strava pattern from that athlete's erg trajectory
(LLM config compiler, cached), then analyze. The result is a portable, committed
`athletes_test.db` so a coach can run the UNCHANGED app per athlete:

    uv run python scripts/gen_all_athletes.py athletes_test.db
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete barrancotto-eve
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete bonnem-lily

The first build needs the rowing mapping (cached in .tokens/ after the first
rowing ingest) and ANTHROPIC_API_KEY for the per-athlete pattern planning; set no
key to fall back to deterministic pattern configs. The committed DB lets testers
skip generation (and Strava) entirely.

Usage:  uv run python scripts/gen_all_athletes.py [db_path]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `uv run python scripts/gen_all_athletes.py` (which puts scripts/ — not the
# repo root — on sys.path) to import the sibling `scripts.multi_athlete` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from ingest import sheet
from ingest.rowing import (
    RowingRoster, _default_llm, _fingerprint, _load_cache, _save_cache,
    extract_activities, infer_mapping,
)
from scripts.multi_athlete import MultiAthleteHarness
from security import crypto

WORKBOOK = Path("rowing_women_2025-2026 ERGS-2.xlsx")


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("athletes_test.db")
    s = get_settings()
    key = crypto.load_or_create_key(s.encryption_key_path)

    # --- resolve the workbook layout once (cached -> offline) ----------------
    tabs = sheet._tabs_preview(WORKBOOK)
    fingerprint = _fingerprint(tabs)
    cache_path = Path(s.synth_token_dir) / "rowing_mapping.enc"
    mapping = _load_cache(cache_path, key, fingerprint)
    if mapping is None:
        mapping = infer_mapping(tabs, llm=_default_llm(s))
        _save_cache(cache_path, key, fingerprint, mapping)

    # --- read every session tab ONCE; share across all athletes --------------
    rows_by_tab = {tab: sheet._rows_from_xlsx(WORKBOOK, tab) for tab in tabs}
    roster = RowingRoster.from_rows(
        rows_by_tab[mapping.roster_tab], mapping.roster_last_col, mapping.roster_first_col)
    athlete_ids = roster.all_athletes()

    def erg_provider(cid: str):
        return extract_activities(tabs, lambda t: rows_by_tab[t], mapping, roster,
                                  chosen_id=cid, athlete_id=cid)

    llm = _default_llm(s) if s.anthropic_api_key else None
    harness = MultiAthleteHarness(
        athlete_ids, erg_provider, llm=llm,
        cache_key=key, token_dir=Path(s.synth_token_dir))
    results = harness.build(db_path)

    # --- summary: the per-athlete spread at a glance -------------------------
    results.sort(key=lambda r: (r.category, r.athlete_id))
    print(f"\nbuilt {db_path}: {len(results)} athletes, "
          f"planner={'LLM' if llm else 'deterministic'}\n")
    print(f"{'athlete':28s} {'pattern':13s} {'erg':>4s} {'sim':>5s} "
          f"{'ergA':>5s} {'trainA':>7s}")
    print("-" * 64)
    for r in results:
        print(f"{r.athlete_id:28s} {r.category:13s} {r.n_erg:4d} "
              f"{r.n_sim_activities:5d} {r.n_erg_anoms:5d} {r.n_train_anoms:7d}")
    from collections import Counter
    by_cat = Counter(r.category for r in results)
    print("\nby pattern:", dict(sorted(by_cat.items())))
    print(f"\nreport on one:  SYNTH_DB_PATH={db_path} uv run synth report --athlete <id>")


if __name__ == "__main__":
    main()
