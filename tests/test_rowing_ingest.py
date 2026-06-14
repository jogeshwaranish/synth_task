"""Rowing pivoted-sheet ingest: deterministic parsers, athlete identity, the
AI-fallback config (validated), end-to-end extraction, and the erg detector.
All offline — the LLM is a stub returning a fixed config."""

from __future__ import annotations

import json
from datetime import date

import pytest

from analyze.rowing import detect_erg_anomalies
from ingest import rowing
from ingest.mapping import TabPreview
from ingest.rowing import (
    RowingIngestError,
    RowingRoster,
    extract_activities,
    infer_mapping,
    parse_clock,
    parse_piece,
    parse_tab_date,
    validate_mapping,
)
from schemas import Activity, AnomalySeverity, Source, Sport


# --- deterministic primitives -----------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("6:35.6", 395.6), ("23:36.4", 1416.4), ("1:45.7", 105.7),
    ("34.0", 34.0), (None, None), ("", None), ("n/a", None),
])
def test_parse_clock(raw, expected):
    assert parse_clock(raw) == expected


@pytest.mark.parametrize("tab,d,label", [
    ("316 2k", date(2026, 3, 16), "2k"),
    ("1027 3x12", date(2025, 10, 27), "3x12"),
    ("98 2x6k", date(2025, 9, 8), "2x6k"),
    ("29 2x6k", date(2026, 2, 9), "2x6k"),
    ("130 6k", date(2026, 1, 30), "6k"),
    ("Names", None, "Names"),
])
def test_parse_tab_date(tab, d, label):
    assert parse_tab_date(tab) == (d, label)


@pytest.mark.parametrize("label,kind,mag", [
    ("2k", "distance_m", 2000.0), ("6K", "distance_m", 6000.0),
    ("2x6k", "distance_m", 12000.0), ("4x1k", "distance_m", 4000.0),
    ("9x2k", "distance_m", 18000.0), ("2k prep", "distance_m", 2000.0),
    ("3x12", "duration_s", 2160.0), ("30", "duration_s", 1800.0),
])
def test_parse_piece(label, kind, mag):
    assert parse_piece(label) == (kind, mag)


# --- identity: isolate ONE athlete across dirty names -----------------------

ROSTER_ROWS = [
    {"Last Name": "Banks", "First Name": "Claire"},
    {"Last Name": "Cox", "First Name": "Madeline"},
    {"Last Name": "Wappler-Niemeyer", "First Name": "Harriet"},
    {"Last Name": "Wheeler", "First Name": "Ella"},
]


@pytest.fixture
def roster():
    return RowingRoster.from_rows(ROSTER_ROWS, "Last Name", "First Name")


@pytest.mark.parametrize("raw,expected", [
    ("Banks, Claire", "banks-claire"),
    ("Banks, Claire ", "banks-claire"),          # trailing whitespace
    ("Cox, Maddy", "cox-madeline"),              # nickname -> canonical
    ("Cox, Madeline", "cox-madeline"),
    ("Wappler-N, Harriet", "wappler-niemeyer-harriet"),  # truncated hyphenated last
    ("Wheeler, Ella ", "wheeler-ella"),
])
def test_roster_resolves_variants_to_one_athlete(roster, raw, expected):
    assert roster.resolve(raw) == expected


@pytest.mark.parametrize("raw", ["Nobody, Jane", "", None, "Smith, Pat"])
def test_roster_rejects_non_athletes(roster, raw):
    assert roster.resolve(raw) is None


# --- the AI-fallback config: validation gates -------------------------------

def _preview() -> dict[str, TabPreview]:
    return {
        "Names": TabPreview(headers=["Last Name", "First Name"],
                            samples=[{"Last Name": "Banks", "First Name": "Claire"}]),
        "316 2k": TabPreview(
            headers=["NAME", "TIME ", "AVG SPLIT", "AVG RATE", "AVG WATTS"],
            samples=[{"NAME": "Banks, Claire", "AVG SPLIT": "1:45.7"}]),
        "98 2x6k": TabPreview(
            headers=["NAME", "AVG SPLIT ", "SPLIT 1", "RATE 1"],
            samples=[{"NAME": "Banks, Claire", "AVG SPLIT ": "2:05.1"}]),
    }


GOOD_CONFIG = {
    "roster_tab": "Names", "roster_last_col": "Last Name",
    "roster_first_col": "First Name", "name_candidates": ["NAME"],
    "split_candidates": ["AVG SPLIT", "AVG SPLIT "],
    "rate_candidates": ["AVG RATE"], "watts_candidates": ["AVG WATTS"],
}


def test_validate_mapping_accepts_good_config():
    m = validate_mapping(GOOD_CONFIG, _preview())
    assert m.roster_tab == "Names"
    assert "AVG SPLIT" in m.split_candidates


@pytest.mark.parametrize("mutate", [
    {"roster_tab": "ghost"},
    {"roster_last_col": "Nope"},
    {"split_candidates": ["not_a_header"]},   # filtered to empty -> reject
    {"name_candidates": []},
])
def test_validate_mapping_rejects_bad_config(mutate):
    bad = {**GOOD_CONFIG, **mutate}
    with pytest.raises(RowingIngestError):
        validate_mapping(bad, _preview())


def test_infer_mapping_uses_stub_llm():
    m = infer_mapping(_preview(), llm=lambda _p: json.dumps(GOOD_CONFIG))
    assert m.name_candidates == ("NAME",)


# --- end-to-end extraction for one athlete ----------------------------------

SESSION_ROWS = {
    "Names": ROSTER_ROWS,
    "98 2x6k": [
        {"NAME": "Wheeler, Ella ", "AVG SPLIT ": "1:52.2"},
        {"NAME": "Banks, Claire", "AVG SPLIT ": "2:05.1", "RATE 1": "23.0"},
    ],
    "316 2k": [
        {"NAME": "Banks, Claire", "AVG SPLIT": "1:45.7", "AVG RATE": "34.0",
         "AVG WATTS": "296.0"},
        {"NAME": "Cox, Maddy", "AVG SPLIT": "1:40.7"},
    ],
}


def _full_preview() -> dict[str, TabPreview]:
    pv = _preview()
    pv["98 2x6k"] = TabPreview(headers=["NAME", "AVG SPLIT ", "RATE 1"],
                               samples=SESSION_ROWS["98 2x6k"][:1])
    return pv


def test_extract_activities_isolates_chosen_athlete():
    m = validate_mapping(GOOD_CONFIG, _full_preview())
    roster = RowingRoster.from_rows(ROSTER_ROWS, "Last Name", "First Name")
    acts = extract_activities(
        _full_preview(), lambda tab: SESSION_ROWS[tab], m, roster,
        chosen_id="banks-claire", athlete_id="banks_claire")

    # Only Banks' rows, across both session tabs (not Wheeler's, not Cox's).
    assert {a.local_date for a in acts} == {date(2025, 9, 8), date(2026, 3, 16)}
    assert all(a.athlete_id == "banks_claire" for a in acts)
    assert all(a.source is Source.SHEET and a.sport is Sport.OTHER for a in acts)

    twok = next(a for a in acts if a.local_date == date(2026, 3, 16))
    assert "2k erg @1:45.7/500m" == twok.name
    assert twok.avg_watts == 296.0
    assert twok.avg_cadence == 34.0
    assert twok.distance_mi == pytest.approx(2000 / 1609.344, abs=1e-3)
    # split 105.7s over 2000m -> ~423s moving time
    assert twok.moving_time_sec == pytest.approx(2000 * 105.7 / 500, abs=0.5)


def test_ingest_rowing_rejects_unknown_athlete():
    pv = _full_preview()
    with pytest.raises(RowingIngestError):
        rowing.ingest_rowing(
            pv, lambda tab: SESSION_ROWS[tab],
            settings=_FakeSettings(), key=b"k" * 32,
            athlete_query="Nobody, Jane", athlete_id="x",
            llm=lambda _p: json.dumps(GOOD_CONFIG))


class _FakeSettings:
    synth_token_dir = "/tmp/synth_rowing_test_tokens_nonexistent"


# --- erg split-trend detector (layer 2) -------------------------------------

def _erg(athlete: str, d: date, piece: str, split_sec: float) -> Activity:
    mph = (500.0 / split_sec) * rowing._MPS_PER_MPH
    return Activity(
        activity_id=f"erg-{athlete}-{d}-{piece}", source=Source.SHEET,
        athlete_id=athlete, start_local=f"{d}T17:00:00", local_date=d,
        name=f"{piece} erg @x/500m", sport=Sport.OTHER,
        moving_time_sec=100.0, distance_mi=1.0, avg_speed_mph=round(mph, 3))


def test_detector_flags_regression_off_best():
    # 2x6k: improves to a best, then a session clearly off it.
    acts = [
        _erg("a", date(2025, 9, 8), "2x6k", 125.0),
        _erg("a", date(2025, 10, 1), "2x6k", 122.0),
        _erg("a", date(2025, 11, 1), "2x6k", 120.0),   # best
        _erg("a", date(2025, 12, 1), "2x6k", 126.0),   # +5% off best -> flag
    ]
    anoms = detect_erg_anomalies(acts)
    reg = [a for a in anoms if a.metric == "erg_split_regression"]
    assert reg and reg[0].severity is AnomalySeverity.FLAG
    assert reg[0].local_date == date(2025, 12, 1)


def test_detector_flags_plateau_when_no_recent_pr():
    acts = [
        _erg("a", date(2025, 9, 8), "6k", 120.0),
        _erg("a", date(2025, 10, 1), "6k", 118.0),     # best
        _erg("a", date(2025, 11, 15), "6k", 118.4),    # not a PR, >21d later
    ]
    plateau = [a for a in detect_erg_anomalies(acts) if a.metric == "erg_split_plateau"]
    assert plateau and plateau[0].severity is AnomalySeverity.WATCH


def test_detector_ignores_sparse_piece():
    acts = [_erg("a", date(2025, 9, 8), "2k", 105.0),
            _erg("a", date(2025, 10, 1), "2k", 110.0)]   # only 2 < MIN
    assert detect_erg_anomalies(acts) == []


# --- layout auto-detection (routes sync to the right ingest) ----------------

def test_detect_layout_triathlon():
    from ingest.sheet import detect_layout
    tabs = {"activities_raw": TabPreview(
        headers=["activity_id", "start_date_local", "sport_type", "moving_time_sec"],
        samples=[{}])}
    assert detect_layout(tabs) == "tri"


def test_detect_layout_rowing_by_roster():
    from ingest.sheet import detect_layout
    tabs = {
        "Names": TabPreview(headers=["Last Name", "First Name"], samples=[{}]),
        "316 2k": TabPreview(headers=["NAME", "AVG SPLIT"], samples=[{}]),
    }
    assert detect_layout(tabs) == "rowing"


def test_detect_layout_rowing_by_name_keyed_tabs():
    from ingest.sheet import detect_layout
    tabs = {
        "316 2k": TabPreview(headers=["NAME", "AVG SPLIT"], samples=[{}]),
        "98 2x6k": TabPreview(headers=["NAME", "AVG SPLIT "], samples=[{}]),
    }
    assert detect_layout(tabs) == "rowing"


def test_detect_layout_defaults_to_tri():
    from ingest.sheet import detect_layout
    assert detect_layout({"misc": TabPreview(headers=["foo", "bar"], samples=[{}])}) == "tri"


class _SyncSettings:
    def __init__(self, tmp_path):
        self.sheet_activities_path = tmp_path / "wb.xlsx"
        self.encryption_key_path = tmp_path / "synth.key"
        self.sheet_kind = None            # unset -> auto-detect fallback
        self.sheet_athlete_query = "Banks, Claire"
        self.strava_athlete_id = "anish"


def test_sync_sheet_routes_to_rowing_and_stamps_unified_athlete(tmp_path, monkeypatch):
    from ingest import sheet
    from store import db
    monkeypatch.setattr(sheet, "_detect_layout", lambda _p: "rowing")
    monkeypatch.setattr(sheet, "_tabs_preview", lambda _p: {})
    captured = {}

    def fake_ingest(tabs, read_rows, *, settings, key, athlete_query, athlete_id, llm=None):
        captured.update(athlete_query=athlete_query, athlete_id=athlete_id)
        return [_erg(athlete_id, date(2026, 1, 1), "2k", 105.0)]

    monkeypatch.setattr(sheet.rowing, "ingest_rowing", fake_ingest)
    conn = db.connect(":memory:")
    db.init_db(conn)
    n = sheet.sync_sheet(_SyncSettings(tmp_path), conn)
    assert n == 1
    # rowing erg rows are stamped with the SAME id as Strava (one athlete, two sources)
    assert captured == {"athlete_query": "Banks, Claire", "athlete_id": "anish"}


def test_sync_sheet_rowing_without_athlete_query_errors(tmp_path, monkeypatch):
    from ingest import sheet
    from store import db
    monkeypatch.setattr(sheet, "_detect_layout", lambda _p: "rowing")
    s = _SyncSettings(tmp_path)
    s.sheet_athlete_query = None
    conn = db.connect(":memory:")
    db.init_db(conn)
    with pytest.raises(RuntimeError, match="SHEET_ATHLETE_QUERY"):
        sheet.sync_sheet(s, conn)


def test_explicit_sheet_kind_overrides_autodetect(tmp_path, monkeypatch):
    """SHEET_KIND=tri must win even if the header heuristic guesses rowing."""
    from ingest import sheet
    from store import db
    monkeypatch.setattr(sheet, "_detect_layout", lambda _p: "rowing")  # would misroute
    monkeypatch.setattr(sheet, "_load_rows", lambda _p, _tab: [])
    monkeypatch.setattr(sheet, "parse_activity_rows", lambda rows, athlete_id: [])
    monkeypatch.setattr(sheet, "_sync_splits", lambda *a: None)

    def _boom(*a, **k):
        raise AssertionError("explicit tri must not route to rowing")
    monkeypatch.setattr(sheet.rowing, "ingest_rowing", _boom)

    s = _SyncSettings(tmp_path)
    s.sheet_kind = "tri"
    s.sheet_wellness_path = None
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert sheet.sync_sheet(s, conn) == 0      # tri path, empty rows


def test_explicit_sheet_kind_forces_rowing(tmp_path, monkeypatch):
    """SHEET_KIND=rowing routes to rowing even if auto-detect says tri."""
    from ingest import sheet
    from store import db
    monkeypatch.setattr(sheet, "_detect_layout", lambda _p: "tri")
    monkeypatch.setattr(sheet, "_tabs_preview", lambda _p: {})
    monkeypatch.setattr(
        sheet.rowing, "ingest_rowing",
        lambda *a, athlete_id, **k: [_erg(athlete_id, date(2026, 1, 1), "2k", 105.0)])
    s = _SyncSettings(tmp_path)
    s.sheet_kind = "rowing"
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert sheet.sync_sheet(s, conn) == 1
