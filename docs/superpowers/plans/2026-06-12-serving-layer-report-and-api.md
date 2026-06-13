# Serving Layer (report + FastAPI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the finished pipeline through `synth report` (CLI) and a thin FastAPI app (`/health`, `/sync`, `/insights`), both delegating to one shared `synthesize/report.generate_report()` that resolves the target athlete/period and drives the agent.

**Architecture:** A new `synthesize/report.py` owns the only new logic — `resolve_target()` (pick athlete + date window from what's in the DB) and `generate_report()` (load key, resolve target, call `run_synthesis`). The CLI command and every FastAPI endpoint are thin wrappers over `report.generate_report` and the existing `sync_*` functions, so all of it is testable offline by injecting a fake Anthropic client or monkeypatching at the wrapper boundary.

**Tech Stack:** Python 3.12, FastAPI + Starlette `TestClient` (already deps), pydantic v2 contract models, stdlib `sqlite3` via `store/db.py`, pytest. No network in tests — the Anthropic client is injected/mocked.

**Spec:** `docs/superpowers/specs/2026-06-09-synth-mvp-design.md` — data-flow step 3 ("`report` → agent → emits SynthesisReport JSON") and the `cli.py / app.py` "thin entry points" responsibility.

## Locked design decisions

- **One shared seam.** `synthesize/report.generate_report(conn, settings, *, athlete=None, start=None, end=None, key=None, client=None) -> SynthesisReport`. CLI and `/insights` both call it; nothing else duplicates target resolution.
- **Target resolution from data, not config.** `resolve_target` defaults the
  athlete to whichever has the most `daily_metrics` rows (so the no-flags case
  "just works" against whatever was ingested — the workbook is `ag`, Strava is
  `basil`). Period defaults to that athlete's full metrics date span. Explicit
  `athlete`/`start`/`end` override. No metrics at all → `ValueError` (caller
  turns it into a clean message / 404).
- **CLI contract:** `synth report` prints the validated `SynthesisReport` as
  JSON on **stdout** (the deliverable); the redacted config line and status go
  to **stderr** so stdout stays parseable. Exit 1 with a clear message when
  there's nothing to report or the model output is rejected.
- **FastAPI is thin and fail-closed.** `/health` is static; `/sync` mirrors the
  CLI's source-skipping; `/insights` calls `generate_report` and maps
  `ValueError`→404, `InsightRejected`→502 (never leak a rejected payload).
  Endpoints import `generate_report`/`sync_*`/`get_settings` at module scope so
  tests monkeypatch them by name.
- **Live model calls only in the real path.** Every test injects a fake client
  or monkeypatches `generate_report`; no test spends tokens.

**File structure:**
- Create: `synthesize/report.py` — `resolve_target` + `generate_report`.
- Modify: `cli.py` — replace the `report` stub with `_cmd_report` + flags.
- Modify: `app.py` — build the FastAPI app with three endpoints.
- Create: `tests/test_report.py` (Task 1–2 unit), `tests/test_app.py` (Task 4–5).
- Modify: `tests/test_sync_and_cli.py` — drop the `report`-stub test, add `report` CLI tests (Task 3).

---

### Task 1: `resolve_target` — pick athlete + period from the store

**Files:**
- Create: `synthesize/report.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_report.py`:

```python
"""resolve_target + generate_report — offline, fake Anthropic client."""

from datetime import date

import pytest

from schemas import DailyMetrics
from store import db
from synthesize.report import resolve_target


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def _metric(d, athlete):
    return DailyMetrics(local_date=date.fromisoformat(d), athlete_id=athlete,
                        rest_day=False)


def test_resolve_target_defaults_to_busiest_athlete_and_full_span(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [
        _metric("2026-06-01", "ag"), _metric("2026-06-05", "ag"),
        _metric("2026-06-03", "ag"), _metric("2026-06-02", "basil"),
    ])
    athlete, start, end = resolve_target(conn, None, None, None)
    assert athlete == "ag"                          # most metrics rows
    assert start == date(2026, 6, 1) and end == date(2026, 6, 5)


def test_resolve_target_honors_explicit_overrides(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [_metric("2026-06-01", "ag"),
                             _metric("2026-06-02", "basil")])
    athlete, start, end = resolve_target(conn, "basil", "2026-06-10", "2026-06-20")
    assert athlete == "basil"
    assert start == date(2026, 6, 10) and end == date(2026, 6, 20)


def test_resolve_target_raises_when_no_metrics(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="run analyze"):
        resolve_target(conn, None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'synthesize.report'`.

- [ ] **Step 3: Write `resolve_target`**

Create `synthesize/report.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_report.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add synthesize/report.py tests/test_report.py
git commit -m "feat(synthesize): resolve_target picks report athlete + period from the store"
```

---

### Task 2: `generate_report` — resolve target, then run the agent

**Files:**
- Modify: `synthesize/report.py`
- Test: `tests/test_report.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_report.py`. Reuse the agent-loop test's fake client by
importing it, so the model call is fully scripted offline:

```python
from datetime import datetime

from config import Settings
from schemas import Activity, Anomaly, AnomalySeverity, Source, Sport
from security import crypto
from synthesize.report import generate_report
from tests.test_agent_loop import FakeClient, _report_json, _text
from types import SimpleNamespace


def _settings(tmp_path):
    return Settings(_env_file=None, anthropic_api_key="k",
                    synth_token_dir=tmp_path / "tok",
                    synth_db_path=tmp_path / "synth.db")


def _seed_full(conn, key):
    start = datetime.fromisoformat("2026-06-03T07:00:00")
    db.upsert_activities(conn, [Activity(
        activity_id="a1", source=Source.SHEET, athlete_id="ag",
        start_local=start, local_date=start.date(), name="Hard intervals",
        sport=Sport.RUN, moving_time_sec=3600, distance_mi=8.0,
    )], key=key)
    db.upsert_metrics(conn, [_metric("2026-06-01", "ag"), _metric("2026-06-07", "ag")])
    db.upsert_anomalies(conn, [Anomaly(
        anomaly_id="ag:2026-06-03:acwr", local_date=date(2026, 6, 3),
        metric="acwr", value=1.6, baseline=1.0, zscore=None,
        severity=AnomalySeverity.FLAG, description="ACWR 1.60 above safe window.",
    )])


def test_generate_report_resolves_target_and_returns_validated_report(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed_full(conn, key)

    client = FakeClient([
        SimpleNamespace(stop_reason="end_turn", content=[_text(_report_json())]),
    ])
    report = generate_report(conn, s, client=client)
    assert report.athlete_id == "ag"
    assert report.report_id and report.contract_version == "1.0"
    # Period was resolved from the stored metrics span and passed through.
    assert report.data_coverage["n_activities"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report.py -q`
Expected: the new test FAILS — `ImportError: cannot import name 'generate_report'`.

- [ ] **Step 3: Write `generate_report`**

Append to `synthesize/report.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_report.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add synthesize/report.py tests/test_report.py
git commit -m "feat(synthesize): generate_report — shared CLI/API seam over run_synthesis"
```

---

### Task 3: `synth report` CLI command

**Files:**
- Modify: `cli.py`
- Test: `tests/test_sync_and_cli.py` (modify + append)

- [ ] **Step 1: Update the stub test + add report tests**

In `tests/test_sync_and_cli.py`, replace `test_cli_report_is_a_stub_for_now`
with the following (the `report` command is no longer a stub):

```python
def test_cli_report_prints_validated_json_to_stdout(tmp_path, monkeypatch, capsys):
    from datetime import date, datetime, timezone
    from schemas import SynthesisReport
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)

    canned = SynthesisReport(
        report_id="r1", generated_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        athlete_id="ag", period_start=date(2026, 6, 1), period_end=date(2026, 6, 7),
        summary="ok", patterns=[],
    )
    seen = {}

    def fake_generate(conn, settings, *, athlete=None, start=None, end=None):
        seen.update(athlete=athlete, start=start, end=end)
        return canned

    monkeypatch.setattr(cli, "generate_report", fake_generate)

    assert cli.main(["report", "--athlete", "ag", "--start", "2026-06-01"]) == 0
    out = capsys.readouterr().out
    parsed = __import__("json").loads(out)           # stdout is pure JSON
    assert parsed["athlete_id"] == "ag" and parsed["report_id"] == "r1"
    assert "HUSH_CLIENT_SECRET" not in out
    assert seen == {"athlete": "ag", "start": "2026-06-01", "end": None}


def test_cli_report_with_no_data_fails_clearly(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)

    def boom(conn, settings, *, athlete=None, start=None, end=None):
        raise ValueError("no daily metrics in the store — run analyze first")

    monkeypatch.setattr(cli, "generate_report", boom)
    assert cli.main(["report"]) == 1
    err = capsys.readouterr().err
    assert "run analyze first" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sync_and_cli.py -q`
Expected: the two new tests FAIL (stub prints "follow-on plan", ignores flags).

- [ ] **Step 3: Wire the command**

In `cli.py`, add imports near the top (after the existing imports):

```python
from synthesize.report import generate_report
from synthesize.validate import InsightRejected
```

Add the command function (place it after `_cmd_analyze`):

```python
def _cmd_report(args: argparse.Namespace) -> int:
    s = get_settings()
    print("config:", s.safe_summary(), file=sys.stderr)  # redacted; keep stdout clean
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    try:
        report = generate_report(conn, s, athlete=args.athlete,
                                 start=args.start, end=args.end)
    except ValueError as e:
        print(f"cannot report: {e}", file=sys.stderr)
        return 1
    except InsightRejected as e:
        print(f"report rejected: {e}", file=sys.stderr)
        return 1
    print(report.model_dump_json(indent=2))              # the deliverable
    return 0
```

In `build_parser()`, replace the `report` stub line:

```python
    report = sub.add_parser("report", help="run the synthesis agent and print a SynthesisReport")
    report.add_argument("--athlete", default=None, help="athlete_id (default: busiest in the DB)")
    report.add_argument("--start", default=None, help="period start YYYY-MM-DD")
    report.add_argument("--end", default=None, help="period end YYYY-MM-DD")
    report.set_defaults(func=_cmd_report)
```

If `_cmd_stub` now has no remaining callers, leave it defined (harmless) — no
other change needed.

- [ ] **Step 4: Run the suite for this file**

Run: `uv run pytest tests/test_sync_and_cli.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_sync_and_cli.py
git commit -m "feat(cli): wire synth report -> generate_report, JSON on stdout"
```

---

### Task 4: FastAPI `/health` + `/sync`

**Files:**
- Modify: `app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app.py`:

```python
"""FastAPI surface — thin wrappers, exercised with Starlette's TestClient."""

from fastapi.testclient import TestClient

import app as app_module
from config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, synth_db_path=tmp_path / "synth.db",
                synth_token_dir=tmp_path / "tok",
                strava_client_id="cid", strava_client_secret="SHH")
    base.update(over)
    return Settings(**base)


def test_health_is_static_and_reports_contract_version():
    client = TestClient(app_module.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "contract_version": "1.0"}


def test_sync_runs_configured_sources_and_returns_counts(tmp_path, monkeypatch):
    s = _settings(tmp_path, sheet_activities_path=tmp_path / "acts.csv")
    monkeypatch.setattr(app_module, "get_settings", lambda: s)
    monkeypatch.setattr(app_module, "sync_strava",
                        lambda settings, conn, *, force_refresh=False: 3)
    monkeypatch.setattr(app_module, "sync_sheet", lambda settings, conn: 8)

    r = TestClient(app_module.app).post("/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["strava"] == 3 and body["sheet"] == 8
    assert body["total_activities"] >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'app'` (placeholder has no FastAPI instance).

- [ ] **Step 3: Build the app with the two endpoints**

Replace the whole body of `app.py`:

```python
"""FastAPI wrapper — a thin HTTP surface over the same functions the CLI calls.

No logic lives here: endpoints delegate to ingest.sync_* and
synthesize.report.generate_report. Local-only, no auth (spec non-goal).
Owner: Basil.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from config import get_settings
from ingest.sheet import sync_sheet
from ingest.strava import sync_strava
from schemas import CONTRACT_VERSION
from store import db
from synthesize.report import generate_report
from synthesize.validate import InsightRejected

app = FastAPI(title="synth")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "contract_version": CONTRACT_VERSION}


@app.post("/sync")
def sync() -> dict:
    s = get_settings()
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    out: dict = {"strava": None, "sheet": None}
    if s.strava_client_id and s.strava_client_secret:
        out["strava"] = sync_strava(s, conn)
    if s.sheet_activities_path is not None:
        out["sheet"] = sync_sheet(s, conn)
    out["total_activities"] = db.count_activities(conn)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat(app): FastAPI /health + /sync thin wrappers"
```

---

### Task 5: FastAPI `/insights`

**Files:**
- Modify: `app.py`
- Test: `tests/test_app.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
from datetime import date, datetime, timezone

from schemas import SynthesisReport


def test_insights_returns_report_json(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)
    canned = SynthesisReport(
        report_id="r1", generated_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        athlete_id="ag", period_start=date(2026, 6, 1), period_end=date(2026, 6, 7),
        summary="ok", patterns=[],
    )
    seen = {}

    def fake_generate(conn, settings, *, athlete=None, start=None, end=None):
        seen.update(athlete=athlete, start=start, end=end)
        return canned

    monkeypatch.setattr(app_module, "generate_report", fake_generate)

    r = TestClient(app_module.app).get("/insights?athlete=ag&start=2026-06-01")
    assert r.status_code == 200
    assert r.json()["athlete_id"] == "ag" and r.json()["report_id"] == "r1"
    assert seen == {"athlete": "ag", "start": "2026-06-01", "end": None}


def test_insights_missing_data_is_404(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)

    def boom(conn, settings, *, athlete=None, start=None, end=None):
        raise ValueError("no daily metrics in the store — run analyze first")

    monkeypatch.setattr(app_module, "generate_report", boom)
    r = TestClient(app_module.app).get("/insights")
    assert r.status_code == 404
    assert "run analyze first" in r.json()["detail"]


def test_insights_rejected_output_is_502(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)

    def boom(conn, settings, *, athlete=None, start=None, end=None):
        raise InsightRejected("schema violation at ['patterns']")

    monkeypatch.setattr(app_module, "generate_report", boom)
    r = TestClient(app_module.app).get("/insights")
    assert r.status_code == 502
    # the rejected payload itself is never echoed back
    assert r.json()["detail"] == "model output failed validation"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -q`
Expected: the 3 new tests FAIL — `/insights` returns 404 (route not found yet).

- [ ] **Step 3: Add the endpoint**

Append to `app.py`:

```python
@app.get("/insights")
def insights(
    athlete: str | None = None, start: str | None = None, end: str | None = None
) -> dict:
    s = get_settings()
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    try:
        report = generate_report(conn, s, athlete=athlete, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InsightRejected:
        # Never echo the rejected payload — it may carry injected/PII content.
        raise HTTPException(status_code=502, detail="model output failed validation")
    return report.model_dump(mode="json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat(app): FastAPI /insights -> generate_report, fail-closed (404/502)"
```

---

### Task 6: DECISIONS.md entry + full-suite verification

**Files:**
- Modify: `DECISIONS.md`

- [ ] **Step 1: Append the decision entry**

Append to `DECISIONS.md`:

```markdown
## Serving layer: one generate_report seam behind both the CLI and the API
`synth report` and FastAPI `/insights` are thin wrappers over
`synthesize/report.generate_report()`, which resolves the target and calls
`run_synthesis`. The non-obvious calls:
- **Target resolved from data, not config.** `resolve_target` defaults the
  athlete to whoever has the most `daily_metrics` rows and the period to that
  athlete's full span, so `synth report` with no flags works against whatever
  was ingested (workbook `ag` vs Strava `basil`). Flags override; no metrics ->
  ValueError -> clean CLI message / HTTP 404.
- **CLI keeps stdout pure JSON:** the report prints to stdout, the redacted
  config + status to stderr, so `synth report | jq` works.
- **API is fail-closed:** `/insights` maps missing data -> 404 and a rejected
  model output -> 502 with a generic detail, never echoing the rejected payload
  (which could carry injected/PII content). `/sync` mirrors the CLI's
  source-skipping; `/health` is static.
- Endpoints/commands import the wrapped functions by name so tests monkeypatch
  them and never touch the network — the agent's injected-client seam keeps the
  whole serving layer offline-testable.
```

- [ ] **Step 2: Full suite**

Run: `uv run pytest -q`
Expected: all pass (existing suite + `test_report.py` + `test_app.py` + the new
CLI report tests). No network touched.

- [ ] **Step 3: Commit**

```bash
git add DECISIONS.md docs/superpowers/plans/2026-06-12-serving-layer-report-and-api.md
git commit -m "docs: record the serving-layer design + plan"
```

- [ ] **Step 4 (optional, spends tokens): live end-to-end smoke**

Only with the real `ANTHROPIC_API_KEY` set in `.env`, and after
`SHEET_ACTIVITIES_PATH=... uv run synth sync` + `uv run synth analyze` have
populated `synth.db`:

```bash
uv run synth report --athlete ag 2>/dev/null | jq '.summary, (.patterns|length), (.evidence|length)'
```

Expected: a non-empty summary, ≥0 patterns, and an evidence trace whose length
equals the number of tool calls the agent actually made. Confirm the JSON
validates (the command already routes through `validate_insight`) and that no
secret or ciphertext appears. This is a manual confidence check, not part of the
automated suite.

---

## Self-Review

**Spec coverage:**
- Data-flow step 3 ("`report` → agent → SynthesisReport JSON") → `generate_report` (Tasks 1–2), `synth report` (Task 3). ✓
- `cli.py`/`app.py` are "thin entry points over the functions above" → CLI and all endpoints delegate to `generate_report`/`sync_*` (Tasks 3–5). ✓
- app.py responsibilities `/health /sync /insights` → Tasks 4–5. ✓
- "Malformed LLM JSON or schema-invalid: rejected + logged, no propagation" (error handling) → `/insights` 502 with generic detail; CLI exit 1 (Tasks 3, 5). ✓
- Security: secrets never logged → CLI prints `safe_summary()` to stderr; `_settings` test asserts the secret isn't in output (Task 3). ✓

**Placeholder scan:** No TBD/"handle errors"/"similar to Task N"; every code step is complete and self-contained.

**Type consistency:** `generate_report(conn, settings, *, athlete=None, start=None, end=None, key=None, client=None)` is defined once (Task 2) and called with exactly these kwargs by the CLI (Task 3) and `/insights` (Task 5); the test doubles use the same signature. `resolve_target(conn, athlete, start, end)` matches its caller in `generate_report`. `run_synthesis(conn, settings, athlete_id, period_start, period_end, *, key, client)` matches the existing signature in `synthesize/agent.py`. `InsightRejected` and `SynthesisReport` are imported from their existing modules. `db.count_activities`, `db.get_metrics`, `sync_strava(s, conn, *, force_refresh=False)`, `sync_sheet(s, conn)` match the existing store/ingest signatures.
```
