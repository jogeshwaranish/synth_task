# Project status & what's left

_Last updated: 2026-06-12 (end of day). Submission due Mon 2026-06-15._

## Where we are

The full pipeline is built, tested, and validated **live** end to end —
ingest → daily join → metrics/anomalies → AI synthesis → CLI + FastAPI.

- **162 tests passing**, fully offline (`uv run pytest -q`).
- **Live-validated** against the real Anthropic API:
  - CLI: `synth sync` (375 sheet activities) → `synth analyze` (141 daily
    metrics, 75 anomalies) → `synth report --athlete ag` → valid report.
  - API over real HTTP: `/health`, `/sync`, `/insights` (404 path **and** the
    happy path — a real ~84s agent run returning a valid `SynthesisReport`).
- **Design decision** (LLM + heuristics) documented in `README.md` + `DECISIONS.md`.
- **Security** (Anish) — all seams finished and integrated: encryption at rest,
  prompt-injection fencing, LLM-output validation, SQL parameterization.

## Done

- [x] Strava ingestion code: OAuth + token cache/rotate + fetch + normalize (unit-tested with mocked HTTP)
- [x] Sheet ingestion: activities, wellness, run/bike/swim splits; LLM column-mapping
- [x] Per-day join (`normalize/join.py`)
- [x] Metrics + anomalies (`analyze/metrics.py`)
- [x] Synthesis agent + 4 tools + harness-owned evidence trace (`synthesize/`)
- [x] CLI: `sync`, `analyze`, `report`
- [x] FastAPI: `/health`, `/sync`, `/insights`
- [x] Security layer (Anish)
- [x] README + design-decision writeup
- [x] Live end-to-end validation (CLI + API)

## What's left (for tomorrow)

### 1. Real Strava data — the main open item (brief requires it)
The brief: _"Each of you should create a Strava account, log a few workouts, and
use your own real data."_ The code is done and dormant; it has never pulled live
data. Steps:
- [ ] Each of us: create a Strava account + log a few workouts.
- [ ] Create a Strava API app (https://www.strava.com/settings/api), callback
      domain `localhost`.
- [ ] Put `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` in `.env`
      (set `STRAVA_ATHLETE_ID` to `basil` / `anish`).
- [ ] `uv run synth sync` → authorizes in the browser, pulls real activities.
- [ ] Re-run `analyze` + `report` and confirm the **cross-source** join works
      (Strava `basil`/`anish` rows alongside sheet `ag` rows). This is the one
      story not yet demonstrated, because the DB is currently sheet-only.

### 2. Architecture / stack diagram
- [ ] Anish started a Figma board (architecture). Add stack + frameworks + icons.
- Note: the Figma link stays **private — not in the repo or anywhere internet-facing** (per Anish). Do not commit it.
- Stack to depict: Python 3.12 · stdlib sqlite3 · httpx · openpyxl · Anthropic
  SDK · FastAPI/uvicorn · pydantic v2 · AES-256-GCM (cryptography).

### 3. Submission packaging (Mon)
- [ ] GitHub repo link
- [ ] README (done — re-skim once Strava is wired)
- [ ] Design decision (done — in README + DECISIONS.md)
- [ ] Per-person contribution note (drafted in README — confirm with Anish)
- [ ] Daily WhatsApp one-liner to AG

## Known caveats (acceptable for a local MVP, but worth noting)

- **Google Sheets is read from a file** (`.xlsx`/CSV export), not the live Sheets
  API. Decide whether AG's "sheet link" needs live API access or an export is
  fine. (`GOOGLE_CREDENTIALS_JSON` / `GOOGLE_SHEET_ID` settings exist as the seam.)
- **`/insights` is synchronous** — a real agent run blocks the request ~30–84s.
  Fine for local/CLI use; would need async/streaming for production.
- **`synth.db` currently holds only `ag` (sheet) data.** Resolves once Strava is wired.
- **`run_segments_raw`** from the workbook is not ingested (no `RunSegment` in the
  contract — would need a `CONTRACT_VERSION` bump + sign-off). Deferred.

## How to pick back up

```bash
uv run pytest -q                                  # confirm green (162)
sqlite3 synth.db "SELECT source, COUNT(*) FROM activity GROUP BY source;"  # see current data
uv run synth report --athlete ag | jq '.summary' # confirm synthesis still works
```

Branch: work on `basil/my-feature`, land on `master` (the trunk). `main` is stale.
