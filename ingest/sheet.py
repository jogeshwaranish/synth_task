"""Sheet ingestion: AG's training workbook, or its per-tab CSV exports.

Parsers are row-oriented (list of dicts), so the file format is isolated to two
thin loaders: stdlib csv and openpyxl. The take-home was distributed as an
xlsx; Basil's local copy is a per-tab CSV export — both must work. The export
already carries converted units (distance_mi, total_elevation_gain_ft,
average_speed_mph): used as-is, no unit math.

UntrustedText fields (name, device_name, wellness notes) originate in the
sheet — DATA, never instructions. They are encrypted at rest by store/db.py
and must be wrapped via synthesize/prompts.wrap_untrusted() before any prompt.
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

Row = dict[str, str | None]

_ACTIVITIES_TAB = "activities_raw"
_WELLNESS_TAB = "health_raw"


def _clean(row: dict) -> Row:
    # Uniform shape for both formats: str values, blanks/None -> None.
    out: Row = {}
    for k, v in row.items():
        if k is None:
            continue  # csv: cells beyond the header row
        s = None if v is None else str(v).strip()
        out[str(k)] = s if s else None
    return out


def _rows_from_csv(path: str | Path) -> list[Row]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [_clean(r) for r in csv.DictReader(f)]


def _norm(v: object) -> object:
    # openpyxl returns integral floats as int (3.0 -> 3); keep the "3.0" form
    # so numeric cells always parse with float() downstream — never int().
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(float(v))
    return v


def _rows_from_xlsx(path: str | Path, tab: str) -> list[Row]:
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        rows_iter = wb[tab].iter_rows(values_only=True)
        header = next(rows_iter, None)
        if header is None:
            return []
        keys = [None if h is None else str(h) for h in header]
        return [
            _clean({k: _norm(v) for k, v in zip(keys, raw)})
            for raw in rows_iter
            if any(v is not None for v in raw)
        ]
    finally:
        wb.close()
