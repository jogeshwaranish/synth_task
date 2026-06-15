"""FastAPI wrapper — a thin HTTP surface over the same functions the CLI calls.

No business logic lives here: endpoints delegate to ingest.sync_*,
synthesize.report.generate_report, and synthesize.render. The browser UI is a
single static document in web.py. Local-only, behind Cloudflare Access — no
auth in-app (spec non-goal). Owners: Basil (API), Anish (frontend + limiter).
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from config import get_settings
from ingest.sheet import sync_sheet
from ingest.strava import sync_strava
from schemas import CONTRACT_VERSION
from store import db
from synthesize.render import render_markdown
from synthesize.report import generate_report
from synthesize.validate import InsightRejected
from web import INDEX_HTML

app = FastAPI(title="synth")

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


@app.get("/insights", dependencies=[Depends(enforce_rate_limit)])
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
    # The harness (not the model) renders the human-readable briefing; ship it
    # alongside the validated JSON contract so the UI can render it directly.
    return {**report.model_dump(mode="json"), "briefing_md": render_markdown(report)}
