"""FastAPI surface — thin wrappers, exercised with Starlette's TestClient."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import web as web_module
from config import Settings
from synthesize.validate import InsightRejected


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test starts with an empty limiter window (so one test's requests
    can't trip the limiter in another) and with the dataset registry pointed at
    throwaway temp DBs (so unit tests never read or mutate the committed
    athletes_test.db fixture)."""
    app_module.rate_limiter.reset()
    monkeypatch.setitem(app_module.DATASETS, "rowing", tmp_path / "rowing.db")
    monkeypatch.setitem(app_module.DATASETS, "triathlon", tmp_path / "triathlon.db")
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


def test_insights_provider_failure_is_502(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)

    def boom(conn, settings, *, athlete=None, start=None, end=None):
        raise RuntimeError("provider overloaded")

    monkeypatch.setattr(app_module, "generate_report", boom)
    r = TestClient(app_module.app).get("/insights")
    assert r.status_code == 502
    assert r.json()["detail"] == "report generation provider unavailable; retry shortly"


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


def test_report_page_renders_status_badge_and_insight_cards():
    # The report view is built client-side from the /insights JSON, so a Python
    # test can only assert the served page carries the markup that builds it: the
    # status badge, the insight cards, and the heuristic that drives the badge.
    body = TestClient(app_module.app).get("/").text
    assert "status-badge" in body          # status badge element
    assert "insight-card" in body          # at least one insight card
    assert "renderReport" in body and "deriveStatus" in body


# --- Client-side render behaviour, exercised by running the real inlined JS ----
# The report view is built in the browser, so to assert *render* behaviour (not
# just source presence) we run the page's own JS under Node with stubbed
# document/marked/DOMPurify and capture the HTML it writes. Skips where Node is
# absent so the suite stays green in a Node-less environment.

_NODE = shutil.which("node")
_INLINE_JS = re.findall(r"<script>(.*?)</script>", web_module.INDEX_HTML, re.S)[-1]
_HARNESS = (
    "const bodyEl = { _html:'', set innerHTML(v){this._html=v;},"
    " get innerHTML(){return this._html;}, classList:{toggle(){},add(){},remove(){}},"
    " addEventListener(){}, value:'', textContent:'', appendChild(){}, min:'', max:'' };\n"
    "global.document = { getElementById: () => bodyEl };\n"
    "global.marked = { parse: (s) => '<p>'+s+'</p>' };\n"
    "global.DOMPurify = { sanitize: (s) => s };\n"
    "global.fetch = async () => ({ ok:true, json: async()=>({athletes:[]}) });\n"
    + _INLINE_JS +
    "\n;renderReport(JSON.parse(process.argv[2]));\n"
    "process.stdout.write(bodyEl._html);\nprocess.exit(0);\n"
)


def _render_report_html(report: dict) -> str:
    if _NODE is None:
        pytest.skip("node not available to execute the inlined report JS")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_HARNESS)
        path = f.name
    try:
        out = subprocess.run([_NODE, path, json.dumps(report)],
                             capture_output=True, text=True, timeout=20)
    finally:
        os.unlink(path)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _compare_periods_evidence(acwr, load, dl_load=0.0):
    digest = ("compare_periods -> " + json.dumps({
        "period_a": {"n_days": 14, "mean_acute_load_7d": load, "mean_acwr": acwr},
        "period_b": {"n_days": 14, "mean_acute_load_7d": load, "mean_acwr": acwr},
        "deltas": {"mean_acute_load_7d": dl_load, "mean_acwr": 0.0},
    }))
    return {"step": 1, "tool": "compare_periods", "args": {}, "result_digest": digest}


def _report(**over):
    base = dict(athlete_id="cox-madeline", period_start="2026-01-01",
                period_end="2026-02-01", data_coverage={}, patterns=[],
                open_questions=[], evidence=[])
    base.update(over)
    return base


def test_metric_strip_omits_tiles_for_missing_data():
    # compare_periods present but with NULL metric values: no tile may render,
    # and the empty strip must be omitted entirely (no placeholder gray boxes).
    html = _render_report_html(_report(evidence=[_compare_periods_evidence(None, None)]))
    assert "metric-tile" not in html      # no tile for a missing metric field
    assert "metric-strip" not in html     # empty strip dropped, not shown empty
    # the pace/HR tiles (never carried in the report) must never appear as dashes
    assert "Erg pace 14d" not in html and "HR @ pace 14d" not in html
    assert "—" not in html                # no dash placeholders anywhere

    # sanity: when the values ARE present, the tiles render
    have = _render_report_html(_report(evidence=[_compare_periods_evidence(1.0, 400.0)]))
    assert "metric-tile" in have and "ACWR" in have


def test_status_badge_not_overreaching_when_acwr_normal():
    # Regression: cox-madeline has ACWR ~1.0 but a high-confidence explanation
    # pattern. The badge must NOT read Overreaching (that was inferred from
    # insight text); it falls through to Plateau.
    report = _report(
        patterns=[dict(pattern_id="p1", title="t", description="d",
                       kind="anomaly_explanation", date_start="2026-01-01",
                       date_end="2026-02-01", metrics_involved=["acwr"],
                       confidence="high", caveats=None)],
        evidence=[_compare_periods_evidence(1.0, 300.0)],
    )
    html = _render_report_html(report)
    assert "Overreaching" not in html
    assert "Plateau" in html and "System read" in html


def test_status_badge_overreaching_only_on_high_acwr():
    html = _render_report_html(_report(evidence=[_compare_periods_evidence(1.45, 500.0)]))
    assert "Overreaching" in html


def test_status_badge_omitted_when_underivable():
    # No ACWR signal and no patterns -> no badge, and so no SYSTEM READ label.
    html = _render_report_html(_report())
    assert "status-badge" not in html
    assert "System read" not in html


def test_report_visualizes_coach_read_and_next_steps():
    report = _report(
        summary=(
            "READ: Keep the athlete steady this week.\n"
            "NEXT_7_DAYS:\n"
            "- Cap hard work at one session.\n"
            "- Protect sleep before the next test.\n"
            "DATA_CONFIDENCE: Training signal is strong; wellness is missing."
        ),
        data_coverage={"n_activities": 12, "n_days": 14, "n_wellness_days": 0},
    )
    html = _render_report_html(report)
    assert "coach-panel" in html
    assert "Coach read" in html
    assert "Keep the athlete steady" in html
    assert "Next 7 days" in html
    assert "Cap hard work at one session" in html
    assert "no wellness rows" in html


def test_index_exposes_overview_and_chart_driven_detail():
    body = TestClient(app_module.app).get("/").text
    assert "Data Snapshot" in body
    assert "/overview" in body
    assert "/athlete-series" in body
    assert "/sync-google-sheet" in body
    assert "Connect Sheet" in body
    assert "Sync + Analyze" in body
    assert "Generate Insight Report" in body
    assert "Querying metrics and anomalies" in body
    assert 'id="sheet-url"' in body
    assert 'id="sheet-athlete-query"' not in body
    assert 'id="sync-summary"' in body
    assert 'id="load-chart"' in body
    assert "Report generation uses the Anthropic API" in body


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


# --- Datasets -------------------------------------------------------------

from schemas import Anomaly, AnomalySeverity, DailyMetrics
from store import db as store_db


def _seed(path, rows):
    conn = store_db.connect(path)
    store_db.init_db(conn)
    store_db.upsert_metrics(conn, rows)
    return path


def test_athletes_lists_per_athlete_date_spans():
    p = app_module.DATASETS["rowing"]
    _seed(p, [
        DailyMetrics(local_date=date(2026, 1, 1), athlete_id="x", rest_day=False),
        DailyMetrics(local_date=date(2026, 1, 9), athlete_id="x", rest_day=False),
        DailyMetrics(local_date=date(2026, 2, 1), athlete_id="y", rest_day=False),
    ])
    r = TestClient(app_module.app).get("/athletes?dataset=rowing")
    assert r.status_code == 200
    athletes = r.json()["athletes"]
    assert {"athlete_id": "x", "start": "2026-01-01",
            "end": "2026-01-09", "n_days": 2} in athletes
    assert any(a["athlete_id"] == "y" for a in athletes)


def test_athletes_unknown_dataset_is_404():
    r = TestClient(app_module.app).get("/athletes?dataset=../secret")
    assert r.status_code == 404
    assert "../secret" not in r.text or "unknown dataset" in r.json()["detail"]


def test_insights_unknown_dataset_is_404(monkeypatch):
    # Rejected by the dataset allowlist before any work happens.
    r = TestClient(app_module.app).get("/insights?athlete=x&dataset=bogus")
    assert r.status_code == 404


def test_insights_routes_to_selected_dataset(tmp_path, monkeypatch):
    _seed(app_module.DATASETS["triathlon"],
          [DailyMetrics(local_date=date(2026, 1, 1),
                        athlete_id="triathlon", rest_day=False)])
    monkeypatch.setattr(app_module, "get_settings", lambda: _settings(tmp_path))
    seen = {}

    def fake(conn, settings, *, athlete=None, start=None, end=None):
        seen["athlete"] = athlete
        return _canned("triathlon")

    monkeypatch.setattr(app_module, "generate_report", fake)
    r = TestClient(app_module.app).get(
        "/insights?athlete=triathlon&dataset=triathlon")
    assert r.status_code == 200
    assert r.json()["athlete_id"] == "triathlon"
    assert seen["athlete"] == "triathlon"


def test_sync_google_sheet_downloads_sheet_into_selected_dataset(tmp_path, monkeypatch):
    s = _settings(tmp_path, sheet_kind="tri", strava_athlete_id="triathlon")
    monkeypatch.setattr(app_module, "get_settings", lambda: s)
    downloaded_to = {}

    def fake_download(sheet_url, destination):
        downloaded_to["sheet_url"] = sheet_url
        downloaded_to["destination"] = destination
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"PK\x03\x04fake")
        return Path(destination)

    def fake_sync(settings, conn):
        assert settings.sheet_activities_path == downloaded_to["destination"]
        assert settings.synth_db_path == app_module.DATASETS["triathlon"]
        assert settings.sheet_kind == "tri"
        assert settings.strava_athlete_id == "triathlon"
        return 4

    monkeypatch.setattr(app_module, "download_sheet_export", fake_download)
    monkeypatch.setattr(app_module, "sync_sheet", fake_sync)
    monkeypatch.setattr(app_module, "_analyze_conn", lambda conn, settings: {
        "daily_rows": 3, "metrics": 3, "anomalies": 1,
    })

    r = TestClient(app_module.app).post("/sync-google-sheet", json={
        "dataset": "triathlon",
        "sheet_url": "https://docs.google.com/spreadsheets/d/1abc_DEF-234/edit",
    })

    assert r.status_code == 200
    assert r.json() == {
        "dataset": "triathlon",
        "athlete_id": "triathlon",
        "sheet": 4,
        "analysis": {"daily_rows": 3, "metrics": 3, "anomalies": 1},
        "total_activities": 0,
    }
    assert downloaded_to["sheet_url"].startswith("https://docs.google.com/")
    assert downloaded_to["destination"].name == "triathlon_google_sheet.xlsx"


def test_sync_google_sheet_ingests_rowing_roster(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_settings", lambda: s)

    def fake_download(sheet_url, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"PK\x03\x04fake")
        return Path(destination)

    captured = {}

    def fake_sync(path, settings, conn):
        captured.update(
            path=path,
            sheet_kind=settings.sheet_kind,
            query=settings.sheet_athlete_query,
            athlete_id=settings.strava_athlete_id,
        )
        return 44

    monkeypatch.setattr(app_module, "download_sheet_export", fake_download)
    monkeypatch.setattr(app_module, "sync_rowing_roster", fake_sync)
    monkeypatch.setattr(app_module, "_analyze_conn", lambda conn, settings: {
        "daily_rows": 190, "metrics": 190, "anomalies": 119,
    })

    r = TestClient(app_module.app).post("/sync-google-sheet", json={
        "dataset": "rowing",
        "sheet_url": "https://docs.google.com/spreadsheets/d/1dwuUatj_rbrztvRI86D-5rgZpc9enXgs/edit",
    })

    assert r.status_code == 200
    assert r.json()["sheet"] == 44
    assert r.json()["athlete_id"] is None
    assert captured["path"].name == "rowing_google_sheet.xlsx"
    assert captured["sheet_kind"] == "rowing"
    assert captured["query"] is None


def test_sync_google_sheet_unknown_dataset_is_404():
    r = TestClient(app_module.app).post("/sync-google-sheet", json={
        "dataset": "../secret",
        "sheet_url": "https://docs.google.com/spreadsheets/d/1abc_DEF-234/edit",
    })
    assert r.status_code == 404


def test_overview_ranks_athletes_by_deterministic_risk():
    p = app_module.DATASETS["rowing"]
    conn = store_db.connect(p)
    store_db.init_db(conn)
    store_db.upsert_metrics(conn, [
        DailyMetrics(local_date=date(2026, 1, 1), athlete_id="steady",
                     acute_load_7d=100, chronic_load_28d=100, acwr=1.0,
                     rest_day=False),
        DailyMetrics(local_date=date(2026, 1, 2), athlete_id="steady",
                     acute_load_7d=95, chronic_load_28d=100, acwr=0.95,
                     rest_day=False),
        DailyMetrics(local_date=date(2026, 1, 1), athlete_id="risk",
                     acute_load_7d=200, chronic_load_28d=100, acwr=2.0,
                     load_zscore_28d=3.4, rest_day=False),
    ])
    store_db.upsert_anomalies(conn, [
        Anomaly(anomaly_id="risk:2026-01-01:acwr",
                local_date=date(2026, 1, 1), metric="acwr", value=2.0,
                severity=AnomalySeverity.FLAG, description="High ACWR"),
        Anomaly(anomaly_id="steady:2026-01-02:acwr",
                local_date=date(2026, 1, 2), metric="acwr", value=0.95,
                severity=AnomalySeverity.WATCH, description="Watch ACWR"),
    ])

    r = TestClient(app_module.app).get("/overview?dataset=rowing")
    assert r.status_code == 200
    body = r.json()
    assert body["dataset"] == "rowing"
    assert [a["athlete_id"] for a in body["athletes"]] == ["risk", "steady"]
    risk = body["athletes"][0]
    assert risk["flag_count"] == 1
    assert risk["watch_count"] == 0
    assert risk["latest"]["acwr"] == 2.0
    assert risk["risk_level"] == "flag"


def test_athlete_series_returns_metrics_and_anomalies_for_charts():
    p = app_module.DATASETS["rowing"]
    conn = store_db.connect(p)
    store_db.init_db(conn)
    store_db.upsert_metrics(conn, [
        DailyMetrics(local_date=date(2026, 1, 1), athlete_id="risk",
                     acute_load_7d=140, chronic_load_28d=100, acwr=1.4,
                     rest_day=False),
        DailyMetrics(local_date=date(2026, 1, 2), athlete_id="risk",
                     acute_load_7d=180, chronic_load_28d=100, acwr=1.8,
                     pace_trend_pct_14d=8.0, rest_day=False),
        DailyMetrics(local_date=date(2026, 1, 2), athlete_id="other",
                     acute_load_7d=80, chronic_load_28d=100, acwr=0.8,
                     rest_day=False),
    ])
    store_db.upsert_anomalies(conn, [
        Anomaly(anomaly_id="risk:2026-01-02:acwr",
                local_date=date(2026, 1, 2), metric="acwr", value=1.8,
                severity=AnomalySeverity.FLAG, description="High ACWR"),
        Anomaly(anomaly_id="other:2026-01-02:acwr",
                local_date=date(2026, 1, 2), metric="acwr", value=0.8,
                severity=AnomalySeverity.WATCH, description="Other athlete"),
    ])

    r = TestClient(app_module.app).get("/athlete-series?dataset=rowing&athlete=risk")
    assert r.status_code == 200
    body = r.json()
    assert body["athlete_id"] == "risk"
    assert [m["local_date"] for m in body["metrics"]] == [
        "2026-01-01", "2026-01-02",
    ]
    assert body["metrics"][1]["pace_trend_pct_14d"] == 8.0
    assert [a["anomaly_id"] for a in body["anomalies"]] == [
        "risk:2026-01-02:acwr",
    ]
