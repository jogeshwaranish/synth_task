"""Tests for sheet settings + sync_sheet wiring (real store, no network)."""

from pathlib import Path

from config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def test_sheet_paths_in_settings_and_safe_summary():
    s = Settings(
        _env_file=None,
        sheet_activities_path=FIXTURES / "sheet_activities_sample.csv",
        sheet_wellness_path=FIXTURES / "sheet_wellness_sample.csv",
    )
    assert s.sheet_activities_path.name == "sheet_activities_sample.csv"
    summary = s.safe_summary()
    assert "sheet_activities_sample.csv" in str(summary["sheet_activities_path"])
    # Defaults stay None (sheet source unconfigured).
    assert Settings(_env_file=None).sheet_activities_path is None


from ingest.sheet import sync_sheet
from security import crypto
from store import db


def _settings(tmp_path, **over):
    defaults = dict(
        _env_file=None,
        synth_token_dir=tmp_path / "tokens",
        synth_db_path=tmp_path / "synth.db",
        sheet_activities_path=FIXTURES / "sheet_activities_sample.csv",
        sheet_wellness_path=FIXTURES / "sheet_wellness_sample.csv",
    )
    defaults.update(over)
    return Settings(**defaults)


def test_sync_sheet_ingests_activities_and_wellness_encrypted(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)

    assert sync_sheet(s, conn) == 8
    assert db.count_activities(conn) == 8

    # PII columns are ciphertext at rest, via the auto-generated machine key.
    raw = conn.execute("SELECT name FROM activity").fetchone()
    assert raw["name"].startswith("enc:")
    key = crypto.load_or_create_key(s.encryption_key_path)
    names = {a.name for a in db.get_activities(conn, key=key)}
    assert "Morning Run" in names

    # Wellness landed too, notes encrypted then readable.
    raw_notes = conn.execute("SELECT notes FROM wellness").fetchall()
    assert all(r["notes"] is None or r["notes"].startswith("enc:") for r in raw_notes)
    days = db.get_wellness(conn, key=key)
    assert len(days) == 3


def test_sync_sheet_is_idempotent(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    sync_sheet(s, conn)
    sync_sheet(s, conn)
    assert db.count_activities(conn) == 8


def test_sync_sheet_without_wellness_path_is_fine(tmp_path):
    s = _settings(tmp_path, sheet_wellness_path=None)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    assert sync_sheet(s, conn) == 8
    assert db.get_wellness(conn) == []


def test_sync_sheet_reads_an_xlsx_workbook(tmp_path):
    from datetime import datetime

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "activities_raw"
    ws.append(["activity_id", "start_date_local", "name", "sport_type",
               "trainer", "moving_time_sec", "distance_mi"])
    ws.append(["X0001", datetime(2026, 6, 7, 7, 0, 0), "Track Intervals",
               "Run", False, 2400, 4.0])
    book = tmp_path / "sheet.xlsx"
    wb.save(book)

    s = _settings(tmp_path, sheet_activities_path=book, sheet_wellness_path=None)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    assert sync_sheet(s, conn) == 1
    key = crypto.load_or_create_key(s.encryption_key_path)
    assert db.get_activities(conn, key=key)[0].name == "Track Intervals"
