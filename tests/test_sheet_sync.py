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
