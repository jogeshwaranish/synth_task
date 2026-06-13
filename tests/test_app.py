"""FastAPI surface — thin wrappers, exercised with Starlette's TestClient."""

from fastapi.testclient import TestClient

import app as app_module
from config import Settings
from synthesize.validate import InsightRejected


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
