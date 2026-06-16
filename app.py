"""FastAPI wrapper — a thin HTTP surface over the same functions the CLI calls.

No business logic lives here: endpoints delegate to ingest.sync_*,
synthesize.report.generate_report, and synthesize.render. The browser UI is a
single static document in web.py. Local-only, behind Cloudflare Access — no
auth in-app (spec non-goal). Owners: Basil (API), Anish (frontend + limiter).
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from analyze.metrics import compute_metrics, detect_anomalies
from analyze.rowing import detect_erg_anomalies
from config import get_settings
from ingest.google_sheet import download_sheet_export
from ingest.rowing import RowingIngestError
from ingest.sheet import sync_rowing_roster, sync_sheet
from ingest.strava import sync_strava
from normalize.join import build_daily_rows
from schemas import CONTRACT_VERSION
from security import crypto
from store import db
from synthesize.render import render_markdown
from synthesize.report import generate_report
from synthesize.validate import InsightRejected
from web import INDEX_HTML

app = FastAPI(title="synth")

# --- Datasets -------------------------------------------------------------
# Two pre-built SQLite fixtures the coach can analyze: the rowing squad and the
# single triathlon athlete (Strava + the triathlon workbook fused under id
# `triathlon`). The browser sends a dataset NAME, never a path — this fixed
# allowlist is the security boundary that stops a client from steering the app
# at an arbitrary file (path traversal / info disclosure). Unknown name → 404.
# TODO(security): paths are read-only fixtures resolved under the repo root. If
# datasets ever become user-supplied or uploadable, sandbox the storage dir and
# re-validate here before trusting any name→path mapping.
_REPO_ROOT = Path(__file__).resolve().parent
DATASETS: dict[str, Path] = {
    "rowing": _REPO_ROOT / "athletes_test.db",
    "triathlon": _REPO_ROOT / "tri_test.db",
}
_DEFAULT_DATASET = "rowing"


def _dataset_path(dataset: str) -> Path:
    path = DATASETS.get(dataset)
    if path is None:
        raise HTTPException(status_code=404, detail=f"unknown dataset '{dataset}'")
    return path


def _dataset_conn(dataset: str):
    """Resolve a dataset NAME to its connection via the fixed allowlist."""
    path = _dataset_path(dataset)
    conn = db.connect(path)
    db.init_db(conn)
    return conn


class GoogleSheetSyncRequest(BaseModel):
    sheet_url: str
    dataset: str = "triathlon"


def _analyze_conn(conn, settings) -> dict[str, int]:
    key = crypto.load_or_create_key(settings.encryption_key_path)
    activities = db.get_activities(conn, key=key)
    wellness = db.get_wellness(conn, key=key)
    daily_rows = build_daily_rows(activities, wellness)
    metrics = compute_metrics(daily_rows)
    anomalies = detect_anomalies(daily_rows, metrics)
    anomalies += detect_erg_anomalies(activities)
    db.upsert_metrics(conn, metrics)
    db.upsert_anomalies(conn, anomalies)
    return {
        "daily_rows": len(daily_rows),
        "metrics": len(metrics),
        "anomalies": len(anomalies),
    }

# --- Per-IP rate limiting -------------------------------------------------
# Each /insights call spends real LLM tokens, so cap callers cheaply with an
# in-process sliding window. slowapi/Redis would be overkill here (and the brief
# forbids new heavy deps), so this is a dependency-free token-less window.
_RATE_LIMIT = 10  # requests ...
_RATE_WINDOW_SEC = 60.0  # ... per this many seconds, per client IP.
_RATE_LIMIT_MESSAGE = (
    "Rate limit exceeded. Each report costs LLM tokens — "
    "please wait before retrying."
)


class SlidingWindowRateLimiter:
    """Allow at most `limit` events per `window` seconds per key (client IP).

    # TODO(security): this state is per-process and in-memory. Behind multiple
    # uvicorn workers or replicas each holds its own window, so the effective
    # limit becomes (n_workers * limit), and it resets on restart. Before any
    # multi-worker / horizontally-scaled deploy, back this with a shared store
    # (e.g. Redis) — do NOT add that dependency without revisiting the brief.
    """

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self._window:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(_RATE_LIMIT, _RATE_WINDOW_SEC)


def enforce_rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT_MESSAGE)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


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


@app.post("/sync-google-sheet")
def sync_google_sheet(payload: GoogleSheetSyncRequest) -> dict:
    s = get_settings()
    dataset_path = _dataset_path(payload.dataset)
    export_path = s.synth_token_dir / "google_sheets" / f"{payload.dataset}_google_sheet.xlsx"
    try:
        sheet_path = download_sheet_export(payload.sheet_url, export_path)
    except (RuntimeError, ValueError, RowingIngestError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    sync_settings = s.model_copy(update={
        "synth_db_path": dataset_path,
        "sheet_activities_path": sheet_path,
        # Same workbook can carry activities + wellness tabs; sync_sheet ignores
        # this for rowing and maps wellness only when relevant.
        "sheet_wellness_path": sheet_path,
    })
    if payload.dataset == "triathlon":
        sync_settings = sync_settings.model_copy(update={
            "sheet_kind": "tri",
            "strava_athlete_id": "triathlon",
        })
    elif payload.dataset == "rowing":
        sync_settings = sync_settings.model_copy(update={
            "sheet_kind": "rowing",
        })
    conn = db.connect(dataset_path)
    db.init_db(conn)
    try:
        n_sheet = (
            sync_rowing_roster(sheet_path, sync_settings, conn)
            if payload.dataset == "rowing"
            else sync_sheet(sync_settings, conn)
        )
    except (RuntimeError, ValueError, RowingIngestError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    analysis = _analyze_conn(conn, sync_settings)
    return {
        "dataset": payload.dataset,
        "athlete_id": sync_settings.strava_athlete_id if payload.dataset == "triathlon" else None,
        "sheet": n_sheet,
        "analysis": analysis,
        "total_activities": db.count_activities(conn),
    }


@app.get("/athletes")
def athletes(dataset: str = _DEFAULT_DATASET) -> dict:
    """Athlete ids + their date coverage, for the form's picker and date bounds.
    Read-only and LLM-free, so it is not rate limited."""
    conn = _dataset_conn(dataset)
    return {"dataset": dataset, "athletes": db.athlete_spans(conn)}


@app.get("/overview")
def overview(dataset: str = _DEFAULT_DATASET) -> dict:
    """LLM-free squad triage rows for the dashboard."""
    conn = _dataset_conn(dataset)
    return {"dataset": dataset, "athletes": db.squad_overview(conn)}


@app.get("/athlete-series")
def athlete_series(athlete: str, dataset: str = _DEFAULT_DATASET) -> dict:
    """Daily metrics + anomalies for one athlete's charts, without LLM cost."""
    conn = _dataset_conn(dataset)
    out = db.athlete_series(conn, athlete)
    if not out["metrics"]:
        raise HTTPException(status_code=404, detail=f"no metrics for athlete '{athlete}'")
    return {"dataset": dataset, **out}


@app.get("/insights", dependencies=[Depends(enforce_rate_limit)])
def insights(
    athlete: str | None = None, start: str | None = None, end: str | None = None,
    dataset: str = _DEFAULT_DATASET,
) -> dict:
    s = get_settings()
    conn = _dataset_conn(dataset)
    try:
        report = generate_report(conn, s, athlete=athlete, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InsightRejected:
        # Never echo the rejected payload — it may carry injected/PII content.
        raise HTTPException(status_code=502, detail="model output failed validation")
    except Exception:
        # Anthropic/network provider failures should not become HTML 500 pages;
        # the UI turns this stable detail into retry guidance.
        raise HTTPException(
            status_code=502,
            detail="report generation provider unavailable; retry shortly",
        )
    # The harness (not the model) renders the human-readable briefing; ship it
    # alongside the validated JSON contract so the UI can render it directly.
    return {**report.model_dump(mode="json"), "briefing_md": render_markdown(report)}
