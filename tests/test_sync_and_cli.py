"""Tests for the sync_strava wiring and the `synth` CLI.

Token + fetch are faked at the function boundary; the store layer underneath is
real SQLite in tmp_path, so these verify the actual wiring: normalize -> encrypt
PII with the per-machine key -> upsert, and the CLI's redaction guarantee.
"""

import time

import cli
from config import Settings
from ingest import strava
from ingest.strava import TokenBundle, sync_strava
from store import db
from security import crypto


def _settings(tmp_path, **over) -> Settings:
    defaults = dict(
        _env_file=None,
        synth_token_dir=tmp_path / "tokens",
        synth_db_path=tmp_path / "synth.db",
        strava_client_id="cid",
        strava_client_secret="HUSH_CLIENT_SECRET",
    )
    defaults.update(over)
    return Settings(**defaults)


def _raw(i: int, name: str) -> dict:
    return {
        "id": i,
        "name": name,
        "start_date_local": "2026-05-13T09:40:31Z",
        "start_date": "2026-05-13T14:40:31Z",
        "sport_type": "Run",
        "distance": 5000,
        "moving_time": 1500,
    }


def _fake_token_and_fetch(monkeypatch, raws: list[dict]) -> None:
    tb = TokenBundle("acc", "ref", int(time.time()) + 3600, "activity:read_all")
    monkeypatch.setattr(strava, "load_or_refresh_token", lambda s, **kw: tb)
    monkeypatch.setattr(strava, "fetch_activities", lambda s, t, **kw: raws)


def test_sync_normalizes_and_stores_with_pii_encrypted(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _fake_token_and_fetch(monkeypatch, [_raw(1, "Run past 123 Main St"), _raw(2, "Tempo")])
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)

    assert sync_strava(s, conn) == 2
    assert db.count_activities(conn) == 2

    # The live path must encrypt UntrustedText at rest — raw cells are ciphertext.
    raw_names = [r[0] for r in conn.execute("SELECT name FROM activity")]
    assert all(n.startswith("enc:") for n in raw_names)
    assert not any("Main St" in n for n in raw_names)

    # ...and reads back to plaintext with the same auto-generated key.
    key = crypto.load_or_create_key(s.encryption_key_path)
    names = {a.name for a in db.get_activities(conn, key=key)}
    assert names == {"Run past 123 Main St", "Tempo"}


def test_sync_is_idempotent_on_activity_id(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _fake_token_and_fetch(monkeypatch, [_raw(1, "Morning Run")])
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)

    sync_strava(s, conn)
    sync_strava(s, conn)  # same activity again -> upsert, not a duplicate row
    assert db.count_activities(conn) == 1


def test_cli_sync_prints_count_and_never_leaks_secrets(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    seen_kwargs: dict = {}

    def fake_sync(settings, conn, *, force_refresh=False):
        seen_kwargs["force_refresh"] = force_refresh
        return 3

    monkeypatch.setattr(cli, "sync_strava", fake_sync)

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "strava: synced 3 activities" in out
    assert seen_kwargs["force_refresh"] is False
    # safe_summary() only — the client secret must never reach stdout.
    assert "HUSH_CLIENT_SECRET" not in out


def test_cli_sync_refresh_flag_forces_token_refresh(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    seen_kwargs: dict = {}

    def fake_sync(settings, conn, *, force_refresh=False):
        seen_kwargs["force_refresh"] = force_refresh
        return 0

    monkeypatch.setattr(cli, "sync_strava", fake_sync)

    assert cli.main(["sync", "--refresh"]) == 0
    assert seen_kwargs["force_refresh"] is True


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


def _stored_run(conn, key, i, day, minutes=60.0):
    from datetime import datetime
    from schemas import Activity, Source, Sport
    start = datetime.fromisoformat(f"{day}T07:00:00")
    db.upsert_activities(conn, [Activity(
        activity_id=f"r{i}", source=Source.SHEET, athlete_id="ag",
        start_local=start, local_date=start.date(), name=f"run {i}",
        sport=Sport.RUN, moving_time_sec=minutes * 60, distance_mi=5.0,
    )], key=key)


def test_cli_analyze_computes_and_persists_metrics_and_anomalies(
    tmp_path, monkeypatch, capsys
):
    from datetime import date, timedelta
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    d0 = date(2026, 5, 1)
    for i in range(28):                          # steady base ...
        _stored_run(conn, key, i, (d0 + timedelta(days=i)).isoformat())
    _stored_run(conn, key, 99, "2026-05-29", minutes=300.0)  # ... then a spike

    assert cli.main(["analyze"]) == 0
    out = capsys.readouterr().out
    assert "29 days" in out and "daily metrics" in out
    assert "HUSH_CLIENT_SECRET" not in out       # safe_summary() only

    metrics = db.get_metrics(conn)
    assert len(metrics) == 29                    # one per calendar day
    assert any(a.metric == "load_zscore_28d" for a in db.get_anomalies(conn))


def test_cli_analyze_with_empty_db_fails_clearly(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    assert cli.main(["analyze"]) == 1
    assert "nothing to analyze" in capsys.readouterr().out


def test_cli_sync_sheet_only_config_skips_strava(tmp_path, monkeypatch, capsys):
    s = _settings(
        tmp_path,
        strava_client_id=None,
        strava_client_secret=None,
        sheet_activities_path=tmp_path / "acts.csv",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    monkeypatch.setattr(cli, "sync_sheet", lambda settings, conn: 8)

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "strava: skipped" in out
    assert "sheet: synced 8 activities" in out


def test_cli_sync_strava_only_config_skips_sheet(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)  # has strava creds, no sheet path
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    monkeypatch.setattr(cli, "sync_strava", lambda settings, conn, *, force_refresh=False: 3)

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "strava: synced 3 activities" in out
    assert "sheet: skipped" in out


def test_cli_sync_with_nothing_configured_fails_clearly(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path, strava_client_id=None, strava_client_secret=None)
    monkeypatch.setattr(cli, "get_settings", lambda: s)

    assert cli.main(["sync"]) == 1
    assert "nothing to sync" in capsys.readouterr().out
