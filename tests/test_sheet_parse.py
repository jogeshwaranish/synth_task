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


from datetime import date, datetime, timezone

import pytest

from ingest.sheet import parse_activity_rows
from schemas import Source, Sport


def test_parse_activities_full_mapping():
    acts = parse_activity_rows(_rows_from_csv(ACTIVITIES_CSV))
    assert len(acts) == 8
    a = acts[0]
    assert a.activity_id == "S0001"
    assert a.source == Source.SHEET
    assert a.athlete_id == "ag"                       # default
    # Non-zero-padded wall-clock parses; local_date derives from it.
    assert a.start_local == datetime(2026, 6, 1, 7, 0, 0)
    assert a.local_date == date(2026, 6, 1)
    assert a.start_utc == datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    assert a.name == "Morning Run"
    assert a.sport == Sport.RUN
    assert a.is_trainer is False
    assert a.moving_time_sec == 1800
    assert a.elapsed_time_sec == 1900
    assert a.distance_mi == 3.0
    assert a.elevation_gain_ft == 98.4
    assert a.avg_speed_mph == 6.0
    assert a.avg_hr == 150 and a.max_hr == 165
    assert a.avg_watts is None                        # blank -> None
    assert a.avg_cadence == 88
    assert a.device_name == "Garmin Forerunner 945"
    assert a.suffer_score == 40 and a.calories == 310
    assert a.perceived_exertion == 6


def test_parse_activities_quirks():
    acts = {a.activity_id: a for a in parse_activity_rows(_rows_from_csv(ACTIVITIES_CSV))}
    assert acts["S0004"].is_trainer is True           # TRUE string
    assert acts["S0004"].sport == Sport.VIRTUAL_RIDE
    # 11:58 PM belongs to the day the athlete experienced (June 2, not UTC June 3).
    assert acts["S0005"].local_date == date(2026, 6, 2)
    assert acts["S0006"].sport == Sport.OTHER         # "Yoga" -> OTHER
    assert acts["S0003"].elapsed_time_sec is None     # blank cell
    assert acts["S0008"].start_utc is None            # blank start_date_utc


def test_parse_activities_respects_athlete_id_param():
    acts = parse_activity_rows(_rows_from_csv(ACTIVITIES_CSV), athlete_id="basil")
    assert all(a.athlete_id == "basil" for a in acts)


def test_malformed_activity_row_raises_with_its_id():
    bad = [{"activity_id": "S9999", "start_date_local": "not-a-date", "name": "x",
            "sport_type": "Run"}]
    with pytest.raises(ValueError, match="S9999"):
        parse_activity_rows(bad)
