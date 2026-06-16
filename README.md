# synth-task

Local backend for the synth MVP: pulls Strava + a coach's spreadsheet,
normalizes into the v1.0 contract (`schemas.py`), stores in SQLite at two grains,
computes training-load metrics + anomalies, and runs an Anthropic agent that
emits a `SynthesisReport` — printed as a human-readable coaching briefing.

> **Just want to see results?** A full multi-athlete dataset is already committed.
> Skip straight to [Quickest test](#quickest-test-everything-is-committed) — no
> Strava, no spreadsheet, no data generation needed.

## Project layout

Flat top-level packages (the contract refers to bare paths like
`analyze/metrics.py`). Data flows left→right: **ingest → normalize → store →
analyze → synthesize**.

| Path | What lives here |
|---|---|
| `schemas.py` | **Locked v1.0 contract** — every Pydantic model that crosses the pipeline (`Activity`, `WellnessDay`, `DailyRow`, `DailyMetrics`, `Anomaly`, `SynthesisReport`). `CONTRACT.md` documents it. |
| `config.py` | `Settings` (env/`.env`): Strava creds, Anthropic model/key, DB + token paths. `safe_summary()` redacts secrets. |
| `ingest/` | Source adapters → contract `Activity`/`WellnessDay`. `strava.py` (API + OAuth), `sheet.py` (triathlon workbook + layout routing), `mapping.py` (LLM column-mapper for odd wellness sheets), `rowing.py` (AI-fallback ingest for the pivoted multi-athlete erg workbook + roster identity). |
| `normalize/` | `join.py` — fuses activities + wellness into one `DailyRow` per athlete-day. |
| `store/` | `db.py` — stdlib `sqlite3`, `?`-bound; field-level AES-256-GCM encryption of untrusted/PII columns at rest. |
| `analyze/` | `metrics.py` (rolling load, ACWR, z-scores, pace/HR-at-pace trends + anomaly detectors), `rowing.py` (additive per-500m erg split-trend detector). |
| `synthesize/` | The agent. `agent.py` (tool loop), `tools.py` (4 read-only DB tools), `prompts.py` (`wrap_untrusted` injection fence), `validate.py` (`validate_insight` — schema-checks LLM output, fail-closed), `report.py` (resolve target + drive agent), `render.py` (report → Markdown briefing). |
| `security/` | `crypto.py` — AES-256-GCM + per-machine key (`.tokens/synth.key`, 0600). |
| `cli.py` / `app.py` | `synth sync\|analyze\|report` CLI · FastAPI (`/health`, `/sync`, `/insights`). |
| `scripts/` | Throwaway test harnesses (NOT shipped): `gen_test_strava.py`, `gen_rowing_test.py`, and the whole-roster `multi_athlete.py` + `gen_all_athletes.py`. |
| `tests/` | `uv run pytest -q` — offline against fixtures, never the network. |
| `*.md` | `CONTRACT.md` (interface), `DECISIONS.md` (one paragraph per tradeoff), `CLAUDE.md` (repo conventions). |

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

## Quickest test (everything is committed)

A full **47-athlete dataset is already committed as `athletes_test.db`** — real
erg results + per-athlete simulated Strava + wellness, with `analyze` already run
and stored. **Nothing to generate, no Strava, no spreadsheet.** The *only* thing a
report needs is `ANTHROPIC_API_KEY` in `.env` (the agent that writes the briefing
is a live LLM call):

    # 1. one-time setup
    uv venv --python 3.12 && uv pip install -e ".[dev]"
    cp .env.example .env          # set ANTHROPIC_API_KEY (Strava/Sheets NOT needed)

    # 2. report on ANY athlete — contrast the three patterns the system finds.
    #    Each report is scoped to that athlete's own worklist; the exact coaching
    #    read is the live agent's call, so wording varies run to run.
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete cox-madeline     # adapting: lightest worklist (~3 flags)
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete bonnem-lily      # plateau: erg stalled + recovery drift
    SYNTH_DB_PATH=athletes_test.db uv run synth report --athlete bosio-giulia     # overreaching: heaviest worklist, back off

    # list every athlete id in the committed DB:
    sqlite3 athletes_test.db "SELECT DISTINCT athlete_id FROM activity ORDER BY 1;"

Add `--format json` for the raw contract object. You do **not** need to run `sync`
or `analyze` against this DB — both are already baked in.

> **Pick a strong model for reports.** The briefing is a live agent call whose
> output is validated against the contract (`insight_schema.json`) and rejected if
> off-contract — never propagated. Cheaper models produce more rejects: a
> 47-athlete sweep on `claude-haiku-4-5` got full reports for **39/47** (the rest
> were transient over-long fields / malformed JSON the validator caught, plus a
> couple of agent crashes on a model-invented impossible date). For reliable
> reports set a stronger model in `.env`, e.g. `ANTHROPIC_MODEL=claude-sonnet-4-6`
> (or Opus). The data, anomalies, and per-athlete worklists are identical
> regardless of model — only the written briefing changes.

## Reading a report

`synth report` prints a Markdown coaching briefing; add `--format json` for the
raw `SynthesisReport` contract object (`schemas.py`) — same content, machine
shape, suitable as the API/deliverable payload. The Markdown has four sections:

| Section | What it is |
|---|---|
| **The big picture** | One-paragraph synthesis — the few things that actually change how this athlete should train/recover. Insight, not a day-by-day recap. |
| **What stands out** | The numbered patterns. Each carries a *kind* (trend / connection / explanation / observation), a **confidence** (low/medium/high), the plain-language takeaway with the supporting numbers, a **"Worth noting"** caveat, and the technical `metrics` it rests on. |
| **Questions to follow up** | Open questions for the coach — what the data can't settle on its own (e.g. was a big day planned, any illness/life stress). |
| **How this was checked** | The **Evidence trail**: every tool call the agent made, in order, with its result digest. |

Two things to know when reading it:

- **The "How this was checked" trail is written by the harness, not the model.**
  The agent can only *request* the four read-only tools; the harness brokers each
  call, runs it against the DB, and records the step. The model cannot forge a
  query it never ran or claim a number it wasn't given — so the trail is an
  auditable record of what the report actually stands on.
- **Every report is validated before you see it.** The model's JSON is checked
  against `insight_schema.json` and rejected if off-contract (bad dates, over-long
  fields, wrong shape) — never propagated. A rejected report is a fail-closed
  safety stop, not a data problem; a stronger model clears them (see the callout
  above).

A trimmed real example (`bonnem-lily`, plateau pattern):

    # Training Insights — bonnem-lily
    *2025-12-24 → 2026-03-11 · 87 activities · 78 wellness days*

    ## The big picture
    Lily is carrying 7+ weeks of accumulated, incompletely-absorbed training
    stress. Resting HR has drifted 44→49 bpm and HRV ~58→50 since late January —
    a sustained signal her body is stressed faster than it recovers... The
    priority now is a genuine recovery week before any further intensity.

    ## What stands out
    ### 1. 7-week drift in recovery markers
    *Trend · high confidence · 2026-01-23 → 2026-03-11*
    Your two overnight recovery signals — resting HR (lower = better) and HRV
    (higher = better) — have both moved the wrong way since late January... a
    7-week trend means a stress debt you haven't paid back. Action: take a
    recovery week (~40% less volume, easy only), then reassess.
    > **Worth noting:** HRV/RHR are single-sensor; illness, alcohol, poor sleep
    > can independently suppress them...
    _Metrics: `hrv`, `rhr`, `load_zscore_28d`_

    ## How this was checked
    The system reviewed 27 flagged data points and ran (trail recorded by the
    system, not the AI):
    1. `get_daily_metrics`(2025-12-24 → 2026-03-11) → 78 rows
    2. `query_anomalies`(—) → 27 rows
    3. `compare_periods`(...) → {deltas: mean_acute_load_7d ...}

### How the committed data was made (rebuild only if you want to change it)

AG's rowing workbook holds ~50 athletes. To show the SAME system surfacing a
**different pattern per athlete**, `scripts/multi_athlete.py` runs the unchanged
pipeline across the whole roster: it ingests each athlete's real erg sessions,
then plants a **lean** slice of simulated Strava shaped by that athlete's own erg
trajectory — an LLM reads the trend and emits a small *validated* pattern config
(adapting / plateau / overreaching), which deterministic code expands into daily
training + wellness. (No app code is modified; see `DECISIONS.md`.)

`athletes_test.db` is stored plaintext so it's portable across machines (the
at-rest key is per-machine). Rebuilding needs `ANTHROPIC_API_KEY` + the workbook:

    uv run python scripts/gen_all_athletes.py athletes_test.db

It prints a per-athlete table (pattern category, erg/sim counts, erg vs training
anomaly counts) so the spread is visible at a glance.

See `docs/superpowers/specs/` for the design and `DECISIONS.md` for tradeoffs.
