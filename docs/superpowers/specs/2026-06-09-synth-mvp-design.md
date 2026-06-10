# synth MVP — Backend Design

**Date:** 2026-06-09
**Owner:** Basil (agentic/synthesis, ingestion, normalization, metrics)
**Collaborator:** Anish (security hardening + validation/data-pipeline layer)
**Contract:** `schemas.py` + `CONTRACT.md` are LOCKED at v1.0. Do not edit without
sign-off; they are the single source of truth for every cross-boundary shape.

## Goal

A local-only backend that pulls Basil's real Strava activities and the founder's
(AG's) Google Sheet, normalizes both into the contract models, stores them in
SQLite at two grains, computes deterministic training-load metrics + anomalies,
and runs an Anthropic agentic loop that investigates anomalies and emits a
`SynthesisReport`. CLI + a thin FastAPI wrapper. Due June 15.

## Non-goals (YAGNI)

- No webhooks (poll on startup + `--refresh`). Runs locally.
- No frontend / no auth on the FastAPI layer (local only).
- No multi-athlete entity model (`athlete_id` stays a plain string per contract).
- No migrations framework — schema is created idempotently on connect.

## Architecture (flat top-level packages at repo root)

Chosen flat (not `src/synth/`) because CONTRACT.md references bare module paths
(`analyze/metrics.py`, `synthesize/prompts.py`) and `from schemas import ...`
stays trivial.

```
config.py            Typed pydantic-settings; secret redaction (safe_summary()).
schemas.py           LOCKED contract.
store/db.py          stdlib sqlite3: connect/migrate, parameterized upsert+query.
ingest/strava.py     OAuth (local redirect) + fetch + normalize -> list[Activity].
ingest/sheet.py      One parser, two sources: .xlsx fixture FIRST, Sheets API 2nd.
normalize/join.py    Unit conversions + daily join -> DailyRow.
analyze/metrics.py   Deterministic loads / ACWR / z-scores / trends + anomalies.
synthesize/prompts.py Delimiter-wrapping of UntrustedText before any prompt.
synthesize/agent.py   Anthropic loop, 4 tools, harness-written evidence trace.
cli.py               sync | analyze | report.
app.py               FastAPI: /sync /insights /health (thin wrapper).
tests/               pytest against the local fixture.
```

### Module responsibilities & interfaces

- **config.py** — loads `.env`; exposes `get_settings()`. Secrets carry
  `repr=False`; `safe_summary()` is the only thing ever logged. Depends on: env.
- **store/db.py** — owns the SQLite schema and all SQL. Public: `connect()`,
  `init_db(conn)`, `upsert_activities()`, `upsert_splits()`, `upsert_wellness()`,
  `upsert_daily_rows()`, `upsert_metrics()`, `upsert_anomalies()`, and matching
  `get_*` readers the agent tools call. All values bound via `?`. Depends on:
  schemas, stdlib sqlite3.
- **ingest/strava.py** — `authorize()` (browser + localhost redirect catch),
  `load_or_refresh_token()` (cache + rotate in `.tokens/`), `fetch_activities()`,
  `to_activity(raw)` normalizer. Output: `list[Activity]`. Depends on: config,
  httpx, schemas.
- **ingest/sheet.py** — `parse_workbook(source)` where source is either the
  local `.xlsx` (openpyxl) or the live Sheets API returning the same row dicts.
  Produces `Activity`, split models, `WellnessDay`. Depends on: schemas, openpyxl.
- **normalize/join.py** — `build_daily_rows(activities, wellness)` applying the
  canonical join rules. Pure function, fully unit-tested. Depends on: schemas.
- **analyze/metrics.py** — `compute_metrics(daily_rows)` -> `list[DailyMetrics]`
  and `detect_anomalies(daily_rows, metrics)` -> `list[Anomaly]`. Pure, no I/O,
  no LLM. Depends on: schemas.
- **synthesize/prompts.py** — `wrap_untrusted(text)` and prompt templates.
  Single choke point for delimiter wrapping. Depends on: nothing.
- **synthesize/agent.py** — `run_synthesis(athlete, period)` driving the
  Anthropic tool loop over the 4 tools (`get_daily_metrics`,
  `get_activity_detail`, `compare_periods`, `query_anomalies`). The HARNESS
  builds `Evidence[]` and fills harness-owned `SynthesisReport` fields; validates
  the model's JSON against `insight_schema.json`, reject+log on failure.
- **cli.py / app.py** — thin entry points over the functions above.

## Data flow

1. `sync` → Strava OAuth/fetch + Sheet parse → normalize → store per-activity
   (+ splits) and `WellnessDay`; then `build_daily_rows` → store `DailyRow`s.
2. `analyze` → read `DailyRow`s → `compute_metrics` + `detect_anomalies` → store.
3. `report` → agent reads anomalies, drills into splits/detail via tools, emits
   `SynthesisReport` JSON. Harness writes `evidence[]`, validates, prints JSON.

## The join (canonical, from CONTRACT.md)

- Two grains: per-activity (+splits) for drill-down; per-day `DailyRow` for join
  + heuristics.
- Join key `local_date` from `start_local`, NEVER UTC (11:58 PM workout belongs
  to the athlete's day).
- Multi-activity days: sum volume fields, duration-weight averages, max maxes.
- Missing wellness → wellness fields `None`, day never dropped (the normal case
  — AG's wellness tabs are empty as of June 9).
- Zero-activity days with wellness still get a `DailyRow` (rest days are signal).

## Metrics (deterministic, analyze/metrics.py)

7d acute load (rolling sum of `training_minutes`), 28d chronic load (28d daily
mean × 7), ACWR (acute/chronic, `None` if <28d history), 28d load z-score, 14d
pace trend % (+ve = slowing), 14d HR-at-pace trend %. Anomalies emitted per the
`Anomaly` model with severities info/watch/flag; `description` is our trusted text.

## Security seams (TODO(security) — Anish plugs in)

Each gets a visible `# TODO(security): ...` at the seam:

- **Input-validation strictness** at the ingest boundary (contract already sets
  `extra="forbid"`; coercion-reject + log hook).
- **Encryption at rest** wrapper around `store/db.py`.
- **Prompt-injection delimiter wrapping** — every `UntrustedText` routed through
  `synthesize/prompts.wrap_untrusted()` before any prompt.
- **SQL parameterization** — `?` binds only, never f-string SQL.
- **LLM output validation** against `insight_schema.json`; failure = reject+log.

## Error handling

- Missing/empty wellness tabs: expected, not an error (never drops a day).
- Token expired/invalid: auto-refresh; on refresh failure, re-run `authorize()`.
- Strava rate limit / non-200: surfaced with status, no secret in the message.
- Malformed LLM JSON or schema-invalid: rejected + logged, no propagation.

## Testing (pytest, against local fixture)

Join: multi-activity day, 11:58 PM local-date boundary, missing wellness,
rest-day-with-wellness. Metrics: ACWR, z-score, pace trend on known inputs.
Strava normalizer: unit conversions (m→mi, m/s→mph, m→ft) on sample rows.

## Decisions (mirror into DECISIONS.md)

- **stdlib sqlite3** over SQLAlchemy: tiny 2-grain schema; keeps the
  parameterization security seam explicit; no dep.
- **Flat layout** over `src/synth/`: honors CONTRACT's bare module paths.
- **uv** over pip-tools: single fast tool + lockfile.
- **Real-data fixture**: `triathlon_sheet.xlsx` is real personal training data
  → repo stays PRIVATE, or the fixture is anonymized before submission. The
  `.xlsx` and the loose CSV export are gitignored until that call is made.

## Build sequencing (review checkpoints)

1. **Skeleton** — pyproject, .gitignore, .env.example, config.py, module stubs
   with TODO(security) seams, CLAUDE.md, DECISIONS.md, generated
   `insight_schema.json`.
2. **Strava end-to-end** — OAuth local-redirect, token cache+rotate, fetch →
   `Activity`, persist. **STOP — Basil tests with the real account.**
3. *(after approval)* Sheet parser → join → metrics → agent → CLI/FastAPI wiring.
