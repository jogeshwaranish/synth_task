"""FastAPI surface — thin wrappers, exercised with Starlette's TestClient."""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import Settings
from synthesize.validate import InsightRejected


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """Each test starts with an empty limiter window so one test's requests
    can't trip the limiter in another."""
    app_module.rate_limiter.reset()
    yield
    app_module.rate_limiter.reset()


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


# --- Web frontend ---------------------------------------------------------

def _canned(athlete: str = "cox-madeline") -> SynthesisReport:
    return SynthesisReport(
        report_id="r1", generated_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        athlete_id=athlete, period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7), summary="Looking strong.", patterns=[],
    )


def test_index_serves_html_page():
    r = TestClient(app_module.app).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    # logotype and the athlete-ID input are both present
    assert "synth." in body
    assert 'id="athlete"' in body


def test_insights_includes_briefing_markdown(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)
    monkeypatch.setattr(app_module, "generate_report",
                        lambda *a, **k: _canned("cox-madeline"))

    r = TestClient(app_module.app).get("/insights?athlete=cox-madeline")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["briefing_md"], str) and body["briefing_md"]
    # rendered from the report, not echoed from the model
    assert "Training Insights — cox-madeline" in body["briefing_md"]
    # the existing JSON contract is still intact alongside the markdown
    assert body["athlete_id"] == "cox-madeline"


def test_insights_unknown_athlete_404_has_no_payload_or_trace(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)

    def boom(conn, settings, *, athlete=None, start=None, end=None):
        raise ValueError(f"no daily metrics for athlete '{athlete}'")

    monkeypatch.setattr(app_module, "generate_report", boom)
    r = TestClient(app_module.app).get("/insights?athlete=nobody")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "Traceback" not in detail and "File \"" not in detail


def test_insights_rate_limited_returns_429(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)
    monkeypatch.setattr(app_module, "generate_report",
                        lambda *a, **k: _canned())

    client = TestClient(app_module.app)
    statuses = [client.get("/insights?athlete=cox-madeline").status_code
                for _ in range(11)]
    assert 429 in statuses
    breached = next(c for c in [client.get("/insights?athlete=cox-madeline")]
                    if c.status_code == 429)
    assert breached.json()["detail"] == (
        "Rate limit exceeded. Each report costs LLM tokens — "
        "please wait before retrying."
    )
