"""Adaptive sheet→contract column mapping. Owner: Anish (data-pipeline seam).

Real athlete workbooks don't share a layout: the wellness data may sit in any
tab under any column names (this export keeps it in `daily_summary`, not the
empty `health_raw` the fixed parser assumed). Rather than hard-code names, an
LLM infers a column→field mapping ONCE per workbook shape; deterministic code
then does the actual parsing and the `?`-bound inserts.

Security (see DECISIONS.md, CLAUDE.md):
- The LLM is a *compiler that emits a mapping config*, NOT a runtime agent with
  DB tools. Only column headers + a few sample cells are shown to it (wrapped via
  synthesize.prompts.wrap_untrusted) — never the full row values, which stay in
  deterministic code.
- Its output is strictly validated (known target fields only, sources must be
  real columns, `local_date` required) before use; invalid → reject + log.
- The inferred mapping is cached encrypted and keyed by a fingerprint of the
  headers, so cost scales per sheet-shape, not per row or per sync.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from schemas import WellnessDay
from security import crypto
from synthesize.prompts import wrap_untrusted

logger = logging.getLogger(__name__)

# target field -> (kind, human description for the prompt, required?)
WELLNESS_TARGETS: dict[str, tuple[str, str, bool]] = {
    "local_date":     ("date",  "calendar date of the record (the join key)", True),
    "in_bed_hours":   ("float", "hours spent in bed", False),
    "asleep_hours":   ("float", "hours actually asleep", False),
    "snoring":        ("float", "snoring measure (units unknown)", False),
    "rhr":            ("float", "resting heart rate, bpm", False),
    "hrv":            ("float", "heart-rate variability, ms", False),
    "body_weight_lb": ("float", "body weight in pounds", False),
    "sauna_mins":     ("float", "minutes spent in sauna", False),
    "notes":          ("text",  "free-text daily notes / comments", False),
}


class MappingRejected(Exception):
    """The inferred mapping failed validation. Never ingest on this."""


@dataclass(frozen=True)
class TabPreview:
    headers: list[str]
    samples: list[dict]  # a few cleaned rows, for the model to disambiguate units


@dataclass(frozen=True)
class WellnessMapping:
    source_tab: str
    columns: dict[str, str]  # target_field -> source column header


# --- inference -------------------------------------------------------------

def _render_tab(tab: str, prev: TabPreview) -> str:
    lines = [f'tab "{tab}"', "columns: " + ", ".join(prev.headers)]
    for i, row in enumerate(prev.samples, 1):
        cells = ", ".join(f"{h}={row.get(h)!r}" for h in prev.headers)
        lines.append(f"sample {i}: {cells}")
    return "\n".join(lines)


def _build_prompt(tabs_preview: dict[str, TabPreview]) -> str:
    targets = "\n".join(
        f"  - {f}: {desc}" + (" (REQUIRED)" if req else "")
        for f, (_kind, desc, req) in WELLNESS_TARGETS.items()
    )
    # Each tab block is untrusted sheet content -> fence it as inert data.
    blocks = "\n\n".join(wrap_untrusted(_render_tab(t, p)) for t, p in tabs_preview.items())
    return (
        "You map spreadsheet columns to a fixed wellness schema. Wellness is the "
        "athlete's daily body/recovery signal (sleep, resting HR, HRV, body "
        "weight, sauna, free-text notes) — NOT workouts and NOT computed training "
        "summaries (miles, power, session counts).\n\n"
        "Target fields — map each source column that clearly fits; OMIT any target "
        "you cannot confidently map:\n" + targets + "\n\n"
        "Here are the workbook tabs with their headers and a few sample rows. Pick "
        "the ONE tab that holds daily wellness/recovery data, and map its columns "
        "to the target fields.\n\n" + blocks + "\n\n"
        "Respond with ONLY a JSON object, no prose or code fences:\n"
        '{"source_tab": "<tab name>", "columns": {"<target_field>": "<exact source column>"}}\n'
        'Every value in "columns" must be an exact header from the chosen tab. '
        '"local_date" is REQUIRED.'
    )


def _extract_json(text: str) -> dict:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("wellness mapping rejected: LLM did not return JSON (%s)", e)
        raise MappingRejected("LLM did not return parseable JSON") from e


def validate_mapping(data: Any, available: dict[str, list[str]]) -> WellnessMapping:
    if not isinstance(data, dict):
        raise MappingRejected("mapping output was not a JSON object")
    tab = data.get("source_tab")
    cols = data.get("columns")
    if tab not in available:
        logger.warning("wellness mapping rejected: unknown source_tab %r", tab)
        raise MappingRejected(f"unknown source_tab {tab!r}")
    if not isinstance(cols, dict):
        raise MappingRejected("'columns' was not an object")
    unknown = set(cols) - set(WELLNESS_TARGETS)
    if unknown:
        logger.warning("wellness mapping rejected: unknown target field(s) %s", sorted(unknown))
        raise MappingRejected(f"unknown target field(s) {sorted(unknown)}")
    headers = set(available[tab])
    for target, src in cols.items():
        if src not in headers:
            logger.warning(
                "wellness mapping rejected: %s -> %r is not a column in %r", target, src, tab
            )
            raise MappingRejected(f"{target} -> {src!r} is not a column in {tab!r}")
    if "local_date" not in cols:
        logger.warning("wellness mapping rejected: required field local_date was not mapped")
        raise MappingRejected("required field local_date was not mapped")
    return WellnessMapping(source_tab=tab, columns=dict(cols))


def infer_wellness_mapping(
    tabs_preview: dict[str, TabPreview], *, llm: Callable[[str], str]
) -> WellnessMapping:
    raw = _extract_json(llm(_build_prompt(tabs_preview)))
    available = {t: p.headers for t, p in tabs_preview.items()}
    return validate_mapping(raw, available)


# --- deterministic apply (row VALUES never go to the LLM) ------------------

def _coerce(kind: str, raw: object) -> object:
    if raw is None:
        return None
    if kind == "date":
        # sheet date cells stringify as "YYYY-MM-DD 00:00:00"; the date part wins.
        return date.fromisoformat(str(raw).split(" ")[0])
    if kind == "float":
        return float(raw)
    return str(raw)  # text (UntrustedText) — encrypted at rest downstream


def apply_wellness_mapping(
    rows: list[dict], mapping: WellnessMapping, *, athlete_id: str = "ag"
) -> list[WellnessDay]:
    out: list[WellnessDay] = []
    for row in rows:
        try:
            kwargs: dict[str, object] = {"athlete_id": athlete_id}
            for target, src in mapping.columns.items():
                kwargs[target] = _coerce(WELLNESS_TARGETS[target][0], row.get(src))
            out.append(WellnessDay(**kwargs))
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            rid = row.get(mapping.columns.get("local_date", "")) or "<missing date>"
            raise ValueError(f"bad wellness row {rid!r}: {e}") from e
    return out


# --- encrypted, fingerprinted mapping cache --------------------------------

def _fingerprint(tabs_preview: dict[str, TabPreview]) -> str:
    payload = json.dumps(
        {t: tabs_preview[t].headers for t in sorted(tabs_preview)}, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(path: Path, key: bytes, fingerprint: str) -> WellnessMapping | None:
    if not path.exists():
        return None
    try:
        data = json.loads(crypto.decrypt(path.read_bytes(), key))
    except Exception:  # corrupt/old cache or wrong key -> re-infer
        return None
    if data.get("fingerprint") != fingerprint:
        return None  # the sheet shape changed; re-infer
    m = data["mapping"]
    return WellnessMapping(source_tab=m["source_tab"], columns=m["columns"])


def _save_cache(path: Path, key: bytes, fingerprint: str, mapping: WellnessMapping) -> None:
    blob = crypto.encrypt(
        json.dumps({
            "fingerprint": fingerprint,
            "mapping": {"source_tab": mapping.source_tab, "columns": mapping.columns},
        }).encode("utf-8"),
        key,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)


def _canonical_mapping(tabs_preview: dict[str, TabPreview]) -> WellnessMapping | None:
    # Fast path: if a tab already uses the contract's own field names, map it
    # directly — no LLM. The LLM is only a fallback for non-conforming layouts,
    # so conformant sheets (and the test fixtures) stay deterministic and offline.
    for tab, prev in tabs_preview.items():
        headers = set(prev.headers)
        if "local_date" in headers:
            return WellnessMapping(
                source_tab=tab,
                columns={t: t for t in WELLNESS_TARGETS if t in headers},
            )
    return None


def _default_llm(settings) -> Callable[[str], str]:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def call(prompt: str) -> str:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    return call


def ingest_wellness(
    tabs_preview: dict[str, TabPreview],
    read_rows: Callable[[str], list[dict]],
    *,
    settings,
    key: bytes,
    athlete_id: str = "ag",
    llm: Callable[[str], str] | None = None,
) -> list[WellnessDay]:
    """Resolve (cache or infer) the wellness mapping, then parse the source tab.

    `read_rows(tab)` returns the cleaned rows for a tab — injected so this module
    stays free of file-format concerns (those live in ingest/sheet.py).
    """
    mapping = _canonical_mapping(tabs_preview)
    if mapping is None:
        # Non-conforming layout: reuse a cached inference or ask the LLM once.
        fingerprint = _fingerprint(tabs_preview)
        cache_path = Path(settings.synth_token_dir) / "wellness_mapping.enc"
        mapping = _load_cache(cache_path, key, fingerprint)
        if mapping is None:
            mapping = infer_wellness_mapping(tabs_preview, llm=llm or _default_llm(settings))
            _save_cache(cache_path, key, fingerprint, mapping)
            logger.info("inferred wellness mapping: tab=%r columns=%s",
                        mapping.source_tab, mapping.columns)
    return apply_wellness_mapping(read_rows(mapping.source_tab), mapping, athlete_id=athlete_id)
