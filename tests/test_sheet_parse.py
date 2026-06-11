"""Tests for ingest/sheet.py — loaders and parsers (no network, synthetic fixtures only)."""

from pathlib import Path

from ingest.sheet import _rows_from_csv, _rows_from_xlsx

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITIES_CSV = FIXTURES / "sheet_activities_sample.csv"


def test_csv_loader_yields_dicts_with_blanks_as_none():
    rows = _rows_from_csv(ACTIVITIES_CSV)
    assert len(rows) == 8
    assert rows[0]["activity_id"] == "S0001"
    assert rows[0]["average_watts"] is None          # blank cell -> None
    assert rows[7]["start_date_utc"] is None          # blank cell -> None
    assert rows[0]["trainer"] == "FALSE"              # strings pass through


def test_xlsx_loader_matches_csv_loader_shape(tmp_path):
    from openpyxl import Workbook
    from datetime import datetime

    wb = Workbook()
    ws = wb.active
    ws.title = "activities_raw"
    ws.append(["activity_id", "start_date_local", "trainer", "distance_mi", "name"])
    # Real xlsx cells carry typed values: datetime, bool, float — not strings.
    ws.append(["S0001", datetime(2026, 6, 1, 7, 0, 0), True, 3.0, "Morning Run"])
    ws.append(["S0002", datetime(2026, 6, 1, 12, 30, 0), False, None, "Lunch Ride"])
    p = tmp_path / "book.xlsx"
    wb.save(p)

    rows = _rows_from_xlsx(p, "activities_raw")
    assert len(rows) == 2
    # Everything is normalized to str|None so parsers are format-agnostic.
    assert rows[0] == {
        "activity_id": "S0001",
        "start_date_local": "2026-06-01 07:00:00",
        "trainer": "True",
        "distance_mi": "3.0",
        "name": "Morning Run",
    }
    assert rows[1]["distance_mi"] is None
