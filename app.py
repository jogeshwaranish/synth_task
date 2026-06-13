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
