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


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        synth_token_dir=tmp_path / "tokens",
        synth_db_path=tmp_path / "synth.db",
        strava_client_secret="HUSH_CLIENT_SECRET",
    )


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
    assert "synced 3 Strava activities" in out
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


def test_cli_analyze_and_report_are_stubs_for_now(capsys):
    assert cli.main(["analyze"]) == 0
    assert "follow-on plan" in capsys.readouterr().out
    assert cli.main(["report"]) == 0
    assert "follow-on plan" in capsys.readouterr().out
