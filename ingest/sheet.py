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


def _rows_from_xlsx(path: str | Path, tab: str) -> list[Row]:
    wb = load_workbook(path, data_only=True)
    try:
        ws = wb[tab]
        rows_iter = ws.iter_rows(values_only=False)
        header_row = next(rows_iter, None)
        if header_row is None:
            return []
        keys = [None if h.value is None else str(h.value) for h in header_row]

        result = []
        for raw in rows_iter:
            if any(cell.value is not None for cell in raw):
                row_dict = {}
                for k, cell in zip(keys, raw):
                    if k is None:
                        continue
                    v = cell.value
                    if v is None:
                        row_dict[str(k)] = None
                    else:
                        # Convert numeric types via float to preserve .0 for whole numbers
                        # (openpyxl reads 3.0 as int 3; float(3) -> 3.0 -> "3.0")
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            row_dict[str(k)] = str(float(v))
                        else:
                            row_dict[str(k)] = str(v)
                result.append(_clean(row_dict))
        return result
    finally:
        wb.close()
