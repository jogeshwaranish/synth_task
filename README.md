# synth-task

Local backend for the synth MVP: pulls Strava + a coach's spreadsheet,
normalizes into the v1.0 contract (`schemas.py`), stores in SQLite at two grains,
computes training-load metrics + anomalies, and runs an Anthropic agent that
emits a `SynthesisReport` — printed as a human-readable coaching briefing.

## Setup
    uv venv --python 3.12 && uv pip install -e ".[dev]"
    cp .env.example .env   # fill in STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET

### ⚠️ Before `sync`: tell it which spreadsheet you have

Set `SHEET_KIND` in `.env` so sync routes to the right ingest. **Always set it
explicitly** — leaving it unset falls back to a header heuristic that can guess
wrong on an unfamiliar workbook.

    # Triathlon workbook (Strava-style activities_raw tab):
    SHEET_ACTIVITIES_PATH=path/to/triathlon.xlsx
    SHEET_KIND=tri

    # Rowing-erg workbook (pivoted, many athletes — must also pick ONE):
    SHEET_ACTIVITIES_PATH=path/to/rowing.xlsx
    SHEET_KIND=rowing
    SHEET_ATHLETE_QUERY=Banks, Claire     # roster name to isolate
    STRAVA_ATHLETE_ID=banks_claire        # id its rows are stamped with

## Usage
    uv run synth sync       # pull Strava + the configured sheet into synth.db
    uv run synth analyze    # compute metrics + anomalies
    uv run synth report     # run the synthesis agent -> readable briefing
    uv run synth report --format json   # same report as the machine deliverable

## Data sources

The athlete's training (Strava) and the coach's spreadsheet are fused into **one
athlete, two sources** — both stamp the same `athlete_id`; provenance lives on
the `source` axis. Point `SHEET_ACTIVITIES_PATH` at a workbook (`.xlsx`) or a
per-tab CSV export.

Declare the workbook **shape** with `SHEET_KIND=tri|rowing` (authoritative). If
you leave it unset, sync falls back to header-based auto-detection
(`ingest/sheet.py::detect_layout`). Either way it routes to the matching ingest:

- **Triathlon layout** — our Strava-style export with an `activities_raw` tab
  (one athlete, dates as rows). Wellness columns that don't match the contract
  are mapped by an LLM once per sheet shape (`ingest/mapping.py`), then cached.
- **Pivoted multi-athlete layout** — e.g. a rowing-erg workbook: a roster tab
  plus one tab per dated test session, each row a different athlete. An LLM
  infers the layout config once (`ingest/rowing.py`), names are canonicalised
  against the roster, and **one** athlete is isolated. Requires
  `SHEET_ATHLETE_QUERY` (e.g. `"Banks, Claire"`); its rows are stamped with
  `STRAVA_ATHLETE_ID` so a real/simulated Strava feed fuses with it. Erg pieces
  land as `Sport.OTHER` Activities; the per-500m split trend is surfaced by an
  additive detector (`analyze/rowing.py`).

In both cases the LLM only ever sees column headers + a few sample cells (fenced
as untrusted via `wrap_untrusted`), never full row values, and its output is
validated before use.

## Testing end-to-end (no real Strava needed)

Two throwaway harnesses build a self-contained test DB so you can exercise the
whole pipeline offline. Each writes a SEPARATE `*.db` (gitignored), so the real
`synth.db` is never touched. `uv run pytest -q` runs the unit suite.

**Rowing** — real erg sheet (AI-fallback ingest, one athlete isolated) + simulated Strava:

    uv run python scripts/gen_rowing_test.py rowing_test.db
    SYNTH_DB_PATH=rowing_test.db uv run synth analyze
    SYNTH_DB_PATH=rowing_test.db uv run synth report --athlete banks_claire

**Triathlon** — generated test Strava, then the real tri workbook fused under the
same athlete (`one athlete, two sources`):

    uv run python scripts/gen_test_strava.py tri_test.db
    SYNTH_DB_PATH=tri_test.db SHEET_KIND=tri \
      SHEET_ACTIVITIES_PATH="Copy of Triathlon Training Sync.xlsx" \
      STRAVA_ATHLETE_ID=anish STRAVA_CLIENT_ID= STRAVA_CLIENT_SECRET= \
      uv run synth sync
    SYNTH_DB_PATH=tri_test.db uv run synth analyze
    SYNTH_DB_PATH=tri_test.db uv run synth report --athlete anish

`report` prints the readable briefing; add `--format json` for the raw contract
object. The first rowing run calls the LLM once to infer the layout, then caches it.

## All athletes at once (committed demo DB)

AG's rowing workbook holds ~50 athletes. To show how the SAME system surfaces a
**different pattern per athlete**, `scripts/multi_athlete.py` runs the unchanged
pipeline across the whole roster: it ingests each athlete's real erg sessions,
then plants a **lean** slice of simulated Strava shaped by that athlete's own erg
trajectory — an LLM reads the trend and emits a small validated pattern config
(adapting / plateau / overreaching), which deterministic code expands into daily
training + wellness. (No app code is modified; see `DECISIONS.md`.)

The build is committed as **`athletes_test.db`** (synthetic Strava + real erg,
stored plaintext so it's portable), so you can test **without any Strava API or
sheet re-ingest** — just report on any athlete:

    # Already built & committed; or rebuild (needs ANTHROPIC_API_KEY + the workbook):
    uv run python scripts/gen_all_athletes.py athletes_test.db

    # Contrast two athletes — clean adaptation vs non-functional overreaching:
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete barrancotto-eve
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete miller-star

The build prints a per-athlete table (pattern category, erg/sim counts, erg vs
training anomaly counts) so the spread is visible at a glance.

See `docs/superpowers/specs/` for the design and `DECISIONS.md` for tradeoffs.
