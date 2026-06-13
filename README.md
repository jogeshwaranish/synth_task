# synth — training & wellness synthesis backend

A local backend that pulls an athlete's **Strava** activities and a **Google
Sheet** of training/wellness data, normalizes both into one locked schema,
stores them in SQLite, computes deterministic training-load metrics and
anomalies, and runs an **Anthropic agent** that investigates those anomalies and
emits an evidence-cited `SynthesisReport`. CLI + a thin FastAPI surface. Local
only, no UI.

Built for the synth intern task (due 2026-06-15). Basil leads the agentic /
synthesis layer; Anish leads security + the data pipeline.

---

## What it does (end to end)

```
Strava API ─┐
            ├─► normalize ─► SQLite ─► metrics + anomalies ─► AI agent ─► SynthesisReport
Google Sheet┘   (contract)   (2 grains)   (deterministic)     (4 tools)     (validated JSON)
```

1. **Ingest** — Strava activities (OAuth + paged fetch) and the sheet (activities,
   wellness, per-activity run/bike/swim splits). The sheet's columns are mapped
   to the contract by an LLM *once per workbook shape*, then parsed
   deterministically.
2. **Normalize + join** — everything becomes the v1.0 contract models
   (`schemas.py`). A per-day join (`DailyRow`) keys on the athlete's *local*
   date, sums volume, duration-weights intensity, and keeps rest days.
3. **Analyze** — deterministic, no LLM: acute/chronic load, ACWR, 28-day load
   z-score, 14-day pace and HR-at-pace trends, and anomaly detection against the
   athlete's *own* rolling baseline.
4. **Synthesize** — an Anthropic agent works the anomaly list using four
   read-only tools (`query_anomalies`, `get_daily_metrics`,
   `get_activity_detail`, `compare_periods`), explains each with evidence, and
   emits a `SynthesisReport`.

## Design decision: LLM + heuristics (not a custom ML model)

We chose **option 1 — an LLM with heuristics**. The reasoning:

- **The data is small and personal.** One athlete with ~months of history is far
  too little to train a trustworthy ML model; any model would overfit and we
  couldn't explain its outputs to a coach.
- **The valuable math is deterministic.** Training load, ACWR, z-scores, and
  trends are well-established sports-science formulas. Computing them in plain,
  tested code makes every number reproducible and auditable — no model needed.
- **The LLM adds what code can't: investigation and narrative.** We use it as an
  *agent over the deterministic outputs* — it forms hypotheses, drills into
  specific activities/splits, compares training blocks, and writes a
  coach-readable explanation where **every claim traces to a real tool call**.
- **Trust boundary.** The deterministic layer is the source of truth; the LLM
  never invents numbers. Its output is validated against the contract schema and
  rejected if off-contract, and the evidence trace is written by our harness, not
  the model — so a hijacked model can't fake what it looked at.

Full rationale and every other tradeoff is in `DECISIONS.md`.

## Security (owner: Anish)

End-to-end, documented in `DECISIONS.md`:

- **At rest:** AES-256-GCM field-level encryption of all PII / free-text columns
  (activity names, devices, wellness notes, swim stroke) and the Strava token
  cache, with a per-machine auto-generated key (never committed). Legacy
  plaintext token caches are migrated on first read.
- **Secrets:** live only in `.env` (gitignored); never logged — only the
  redacted `Settings.safe_summary()` is ever printed.
- **Prompt injection:** every piece of untrusted free text is wrapped in a
  unique-nonce fence as inert DATA before it reaches a prompt (`wrap_untrusted`).
- **LLM output:** validated against `insight_schema.json`; off-contract output is
  rejected and logged, never propagated. Harness-owned fields (the evidence
  trace, report identity) are stripped from model output and supplied
  authoritatively.
- **SQL:** every value is bound with `?` — no string-formatted SQL anywhere.

## Setup

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env        # fill in credentials (see below)
```

Minimum to run synthesis: set `ANTHROPIC_API_KEY` in `.env`. To ingest the
sheet, set `SHEET_ACTIVITIES_PATH` (the `.xlsx` workbook or a per-tab CSV
export). To ingest Strava, set `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`.

## Running it

```bash
# 1. pull configured sources into synth.db (skips any source not configured)
SHEET_ACTIVITIES_PATH="Copy of Triathlon Training Sync.xlsx" uv run synth sync

# 2. compute metrics + anomalies
uv run synth analyze

# 3. run the AI synthesis and print the report  (calls the Anthropic API)
uv run synth report --athlete ag
uv run synth report --athlete ag | jq '.summary, .patterns[].title'
```

Or via the API:

```bash
uv run uvicorn app:app --port 8000
curl localhost:8000/health
curl -X POST localhost:8000/sync
curl "localhost:8000/insights?athlete=ag"     # calls the Anthropic API
# interactive docs at http://localhost:8000/docs
```

## Understanding the output

`synth report` / `/insights` returns a `SynthesisReport` (JSON):

- **`summary`** — a narrative of the athlete's training across the period.
- **`patterns`** — up to 10 findings (`trend` / `correlation` /
  `anomaly_explanation` / `observation`), each with a date range, the metrics
  involved, supporting activity ids, a confidence level, and caveats.
- **`anomalies_reviewed`** — which deterministic anomalies the agent examined.
- **`evidence`** — the agent's actual tool-call trace (written by the harness),
  so every conclusion is traceable to the data it looked at.
- **`data_coverage`** + identity fields (`report_id`, `generated_at`,
  `contract_version`) — filled by the harness, not the model.

## Testing

```bash
uv run pytest -q          # 162 tests, fully offline (no network, no tokens)
```

The suite covers ingestion, the join, every metric and anomaly detector, the
agent loop (against a scripted fake model), the security seams, and the API
endpoints. The full CLI and API paths have also been validated **live** against
the real Anthropic API end to end.

## Layout

```
config.py     settings + secret redaction       store/      SQLite (encrypted PII)
schemas.py    LOCKED v1.0 contract               analyze/    metrics + anomalies
ingest/       strava + sheet + LLM col-mapping   synthesize/ agent, tools, prompts, validate, report
normalize/    per-day join                       cli.py      sync | analyze | report
app.py        FastAPI: /health /sync /insights   tests/      pytest (local, offline)
```

`schemas.py` + `CONTRACT.md` are the locked cross-boundary contract. See
`docs/superpowers/specs/` for designs and `docs/superpowers/plans/` for the
build plans.

## Contributions

- **Basil** — agentic/synthesis layer, ingestion, normalization, metrics &
  anomalies, CLI/API wiring.
- **Anish** — security hardening (encryption at rest, prompt-injection defense,
  LLM-output validation, SQL parameterization) and the data-pipeline/validation
  seams.

## Status / known gaps

See `docs/STATUS.md` for the current state and what's left. In short: the full
pipeline works end to end on the sheet data and is live-validated; the remaining
items are wiring **real Strava data** (each of us pulls our own account) and
final submission polish.
