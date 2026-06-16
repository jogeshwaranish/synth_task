"""Google Sheets link export for the sheet ingest pipeline.

This deliberately downloads the live Sheet as an xlsx export, then lets
ingest/sheet.py handle parsing, mapping, encryption, and storage. For the demo
path this keeps Google connectivity as a thin source adapter instead of a second
spreadsheet parser.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx


_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def extract_sheet_id(sheet_ref: str) -> str:
    """Accept a Google Sheets URL or raw spreadsheet id."""
    ref = sheet_ref.strip()
    if not ref:
        raise ValueError("Google Sheet link is required")

    if _SHEET_ID_RE.fullmatch(ref) and "/" not in ref:
        return ref

    parsed = urlparse(ref)
    parts = [p for p in parsed.path.split("/") if p]
    if "spreadsheets" in parts and "d" in parts:
        d_idx = parts.index("d")
        if d_idx + 1 < len(parts) and _SHEET_ID_RE.fullmatch(parts[d_idx + 1]):
            return parts[d_idx + 1]

    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and _SHEET_ID_RE.fullmatch(query_id):
        return query_id

    raise ValueError("Could not find a Google Sheet id in the provided link")


def export_url(sheet_ref: str) -> str:
    sheet_id = extract_sheet_id(sheet_ref)
    return (
        "https://docs.google.com/spreadsheets/d/"
        f"{quote(sheet_id, safe='')}/export?format=xlsx"
    )


def download_sheet_export(
    sheet_ref: str,
    destination: str | Path,
    *,
    timeout: float = 30.0,
) -> Path:
    """Download a public/shared Google Sheet as an xlsx file.

    The direct export endpoint works when the sheet is shared with link-view
    access. Private sheets will return an HTML sign-in/error page; reject that
    loudly so the caller can ask for link access or add an authenticated source.
    """
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = export_url(sheet_ref)
    response = httpx.get(url, follow_redirects=True, timeout=timeout)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        raise RuntimeError(
            f"Google Sheet export failed with HTTP {status}. "
            "Share the sheet with link-view access and try again."
        ) from e

    if not response.content.startswith(b"PK"):
        raise RuntimeError(
            "Google Sheet export did not return an xlsx file. "
            "Share the sheet with link-view access and try again."
        )

    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(response.content)
    os.replace(tmp, dest)
    return dest
