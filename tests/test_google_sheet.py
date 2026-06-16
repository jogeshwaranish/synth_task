from pathlib import Path

import httpx
import pytest

from ingest import google_sheet


def test_extract_sheet_id_accepts_google_sheet_link():
    link = "https://docs.google.com/spreadsheets/d/1abc_DEF-234/edit#gid=0"
    assert google_sheet.extract_sheet_id(link) == "1abc_DEF-234"


def test_download_sheet_export_writes_xlsx(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, *, follow_redirects, timeout):
        seen.update(url=url, follow_redirects=follow_redirects, timeout=timeout)
        return httpx.Response(
            200,
            content=b"PK\x03\x04fake workbook",
            headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(google_sheet.httpx, "get", fake_get)

    out = google_sheet.download_sheet_export(
        "https://docs.google.com/spreadsheets/d/1abc_DEF-234/edit",
        tmp_path / "sheet.xlsx",
    )

    assert out == tmp_path / "sheet.xlsx"
    assert out.read_bytes() == b"PK\x03\x04fake workbook"
    assert "/spreadsheets/d/1abc_DEF-234/export" in seen["url"]
    assert "format=xlsx" in seen["url"]


def test_download_sheet_export_rejects_private_or_non_xlsx_response(tmp_path, monkeypatch):
    def fake_get(url, *, follow_redirects, timeout):
        return httpx.Response(
            200,
            content=b"<html>Sign in</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(google_sheet.httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="not return an xlsx"):
        google_sheet.download_sheet_export("1abc_DEF-234", tmp_path / "sheet.xlsx")
