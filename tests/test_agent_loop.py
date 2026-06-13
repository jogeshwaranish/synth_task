"""run_synthesis driven by a scripted fake Anthropic client — fully offline."""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from config import Settings
from schemas import Activity, Anomaly, AnomalySeverity, Source, Sport
from security import crypto
from store import db
from synthesize.agent import run_synthesis
from synthesize.validate import InsightRejected


def _text(s):
    return SimpleNamespace(type="text", text=s)


def _tool_use(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


class FakeClient:
    """Scripts successive .messages.create(...) responses."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return outer._scripted.pop(0)

        self.messages = _Messages()


def _report_json():
    return json.dumps({
        "athlete_id": "ag",
        "period_start": "2026-06-01",
        "period_end": "2026-06-07",
        "summary": "Load spiked mid-week; otherwise nominal.",
        "patterns": [{
            "pattern_id": "p1", "title": "Acute load spike",
            "description": "ACWR exceeded 1.5 on 2026-06-03.",
            "kind": "anomaly_explanation",
            "date_start": "2026-06-01", "date_end": "2026-06-07",
            "metrics_involved": ["acwr"], "supporting_activity_ids": ["a1"],
            "confidence": "medium", "caveats": None,
        }],
        "anomalies_reviewed": ["ag:2026-06-03:acwr"],
        "open_questions": [],
    })


def _settings(tmp_path):
    return Settings(_env_file=None, anthropic_api_key="k",
                    synth_token_dir=tmp_path / "tok",
                    synth_db_path=tmp_path / "synth.db")


def _seed(conn, key):
    start = datetime.fromisoformat("2026-06-03T07:00:00")
    db.upsert_activities(conn, [Activity(
        activity_id="a1", source=Source.SHEET, athlete_id="ag",
        start_local=start, local_date=start.date(), name="Hard intervals",
        sport=Sport.RUN, moving_time_sec=3600, distance_mi=8.0,
    )], key=key)
    db.upsert_anomalies(conn, [Anomaly(
        anomaly_id="ag:2026-06-03:acwr", local_date=date(2026, 6, 3),
        metric="acwr", value=1.6, baseline=1.0, zscore=None,
        severity=AnomalySeverity.FLAG, description="ACWR 1.60 above safe window.",
    )])


def test_run_synthesis_happy_path_builds_report_and_evidence(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)

    client = FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            _text("Let me check the worklist."),
            _tool_use("t1", "query_anomalies", {"severity": "flag"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[_text(_report_json())]),
    ])

    report = run_synthesis(
        conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
        key=key, client=client,
    )

    assert report.athlete_id == "ag"
    assert report.contract_version == "1.0"
    assert report.report_id                       # harness-generated, non-empty
    assert report.patterns[0].pattern_id == "p1"
    # Evidence is the REAL trace the harness brokered, one step per tool call.
    assert [e.tool for e in report.evidence] == ["query_anomalies"]
    assert report.evidence[0].step == 1
    assert "query_anomalies" in report.evidence[0].result_digest
    assert report.data_coverage["n_activities"] == 1
    # Second model call carried the tool result back.
    assert len(client.calls) == 2


def test_model_cannot_forge_evidence_or_identity(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)

    forged = json.loads(_report_json())
    forged["evidence"] = [{"step": 99, "tool": "query_anomalies",
                           "args": {}, "result_digest": "FAKE — never ran"}]
    forged["report_id"] = "attacker-chosen"
    client = FakeClient([
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text(json.dumps(forged))]),
    ])

    report = run_synthesis(conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
                           key=key, client=client)
    # No tools were called this run, so the harness trace is empty — the
    # model's forged Evidence and report_id are dropped.
    assert report.evidence == []
    assert report.report_id != "attacker-chosen"


def test_malformed_final_json_is_rejected(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)
    client = FakeClient([
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text("not json at all")]),
    ])
    with pytest.raises(InsightRejected):
        run_synthesis(conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
                      key=key, client=client)


def test_fenced_json_is_extracted(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)
    client = FakeClient([
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text("```json\n" + _report_json() + "\n```")]),
    ])
    report = run_synthesis(conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
                           key=key, client=client)
    assert report.summary.startswith("Load spiked")


def test_unknown_tool_call_yields_no_evidence_and_keeps_going(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)
    client = FakeClient([
        SimpleNamespace(stop_reason="tool_use",
                        content=[_tool_use("t1", "rm_rf_tool", {"x": 1})]),
        SimpleNamespace(stop_reason="end_turn", content=[_text(_report_json())]),
    ])
    report = run_synthesis(conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
                           key=key, client=client)
    assert report.evidence == []          # bogus tool recorded nothing


def test_exhausting_iterations_raises(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    _seed(conn, key)
    looping = [SimpleNamespace(stop_reason="tool_use",
                               content=[_tool_use(f"t{i}", "query_anomalies", {})])
               for i in range(5)]
    client = FakeClient(looping)
    with pytest.raises(InsightRejected):
        run_synthesis(conn, s, "ag", date(2026, 6, 1), date(2026, 6, 7),
                      key=key, client=client, max_iterations=3)
