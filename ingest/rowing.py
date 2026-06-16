"""Flexible AI-fallback ingest for a PIVOTED, multi-athlete workbook.

Owner: Anish (data-pipeline seam). This is the general-schema sibling of
ingest/mapping.py. AG's rowing-erg workbook is shaped nothing like our
triathlon export: it is *pivoted* — one tab per erg TEST SESSION (the date is
encoded in the tab name, e.g. "316 2k" = Mar 16, 2k piece), and within each
tab every ROW is a different athlete. Column layouts drift tab to tab.

Our schema is the inverse (one athlete, dates as rows, stable headers). So:
  1. An LLM infers a mapping CONFIG once per workbook shape (which tab is the
     roster, which column holds the athlete name, which headers carry the
     split/rate/watts signal). The LLM is a compiler that emits config — never
     a runtime agent, and only headers + a few sample cells (wrapped via
     wrap_untrusted) are shown to it, never full row values.
  2. Deterministic code does identity resolution + parsing + the contract maps.

Two genuinely new capabilities AG asked us to prove out:
  - the AI fallback works on a schema "nowhere similar" to ours;
  - the app isolates ONE athlete across many — canonicalising dirty names
    ("Cox, Maddy" vs "Cox, Madeline", trailing spaces, "Wappler-N" vs
    "Wappler-Niemeyer") against the roster, and rejecting names not on it.

Erg pieces land as Sport.OTHER Activities (the contract is LOCKED; no Sport.ROW).
The per-500m split rides in avg_speed_mph (derived) and in the activity name, so
the tool-using agent can read and trend it; the erg split-trend ANOMALY detector
lives in analyze/rowing.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ingest.mapping import TabPreview, _render_tab  # reuse the preview renderer
from schemas import Activity, Source, Sport
from security import crypto
from synthesize.prompts import wrap_untrusted

logger = logging.getLogger(__name__)

# A stroke-rate/split workbook always carries these signals under *some* header.
# The LLM maps the exact headers; these are the contract targets it maps onto.
_MPS_PER_MPH = 2.2369362920544
_M_PER_MI = 1609.344


class RowingIngestError(Exception):
    """Ingest cannot proceed: bad mapping, unknown athlete, or empty result."""


# --- the config the LLM emits (validated before any use) --------------------

@dataclass(frozen=True)
class RowingMapping:
    roster_tab: str
    roster_last_col: str
    roster_first_col: str
    # Header *candidates* per field — code picks the first present in each tab,
    # because session tabs name the same signal differently ("AVG SPLIT" /
    # "AVERAGE" / "SPLIT"). Order = preference.
    name_candidates: tuple[str, ...]
    split_candidates: tuple[str, ...]
    rate_candidates: tuple[str, ...]
    watts_candidates: tuple[str, ...]


# --- deterministic primitives (testable, offline) --------------------------

def parse_clock(raw: object) -> float | None:
    """'6:35.6' -> 395.6 sec; '23:36.4' -> 1416.4; '1:45.7' -> 105.7. Bare
    seconds pass through. Returns None for blanks/garbage (never raises)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if ":" in s:
            mins, secs = s.split(":", 1)
            return int(mins) * 60 + float(secs)
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_tab_date(tab: str) -> tuple[date | None, str]:
    """Tab name encodes M/DD or MM/DD then a piece label: '316 2k' -> (Mar 16,
    '2k'); '1027 3x12' -> (Oct 27, '3x12'); '98 2x6k' -> (Sep 8, '2x6k').
    Season Sep-Dec = 2025, Jan-Aug = 2026. Returns (None, tab) if undecodable."""
    m = re.match(r"\s*(\d{2,4})\s*(.*)$", tab)
    if not m:
        return None, tab.strip()
    digits, label = m.group(1), m.group(2).strip()
    if len(digits) == 2:        # "98" -> 9/8, "29" -> 2/9
        mo, da = int(digits[0]), int(digits[1])
    elif len(digits) == 3:      # "316" -> 3/16, "130" -> 1/30
        mo, da = int(digits[0]), int(digits[1:])
    elif len(digits) == 4:      # "1027" -> 10/27
        mo, da = int(digits[:2]), int(digits[2:])
    else:
        return None, label
    year = 2025 if mo >= 9 else 2026
    try:
        return date(year, mo, da), label
    except ValueError:
        return None, label


def parse_piece(label: str) -> tuple[str, float | None]:
    """Piece label -> ('distance_m', metres) | ('duration_s', secs) |
    ('unknown', None). '2k'->2000m, '2x6k'->12000m, '4x1k'->4000m,
    '2k prep'->2000m, '3x12'->2160s (3x12min), '30'->1800s (30min)."""
    s = re.sub(r"[^0-9a-z]", "", label.lower()).replace("prep", "")
    m = re.fullmatch(r"(?:(\d+)x)?(\d+)k", s)        # NxDk or Dk
    if m:
        n = int(m.group(1) or 1)
        return "distance_m", float(n * int(m.group(2)) * 1000)
    m = re.fullmatch(r"(\d+)x(\d+)", s)              # NxM minutes
    if m:
        return "duration_s", float(int(m.group(1)) * int(m.group(2)) * 60)
    m = re.fullmatch(r"(\d+)", s)                    # M minutes
    if m:
        return "duration_s", float(int(m.group(1)) * 60)
    return "unknown", None


def _split_to_mph(split_sec: float) -> float:
    return (500.0 / split_sec) * _MPS_PER_MPH


def _clock(split_sec: float) -> str:
    return f"{int(split_sec // 60)}:{split_sec % 60:04.1f}"


# --- identity: resolve a dirty session name to a canonical roster athlete ----

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


@dataclass(frozen=True)
class RowingRoster:
    # last_norm -> list of (first_norm, canonical_id)
    _by_last: dict[str, list[tuple[str, str]]]

    @staticmethod
    def from_rows(rows: list[dict], last_col: str, first_col: str) -> "RowingRoster":
        by_last: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            last, first = r.get(last_col), r.get(first_col)
            if not last:
                continue
            ln, fn = _norm(str(last)), _norm(str(first or ""))
            cid = f"{ln}-{fn}".strip("-").replace(" ", "_")
            by_last.setdefault(ln, []).append((fn, cid))
        return RowingRoster(_by_last=by_last)

    def all_athletes(self) -> list[str]:
        """Every canonical athlete id on the roster, sorted + de-duplicated.
        Read-only convenience for the multi-athlete harness (no behaviour change
        to the single-athlete ingest path)."""
        return sorted({cid for entries in self._by_last.values()
                       for _, cid in entries})

    def resolve(self, raw_name: str | None) -> str | None:
        """Map a session-tab name ('Last, First', possibly dirty) to a roster
        canonical_id, or None if it is not a single known athlete."""
        if not raw_name:
            return None
        n = _norm(str(raw_name))
        last, first = (n.split(",", 1) + [""])[:2]
        last, first = last.strip(), first.strip()
        cands = self._by_last.get(last)
        if cands is None:           # fuzzy last-name match (hyphenated/truncated)
            for rl, entries in self._by_last.items():
                if (len(last) >= 4 and (rl.startswith(last) or last.startswith(rl))):
                    cands = entries
                    last = rl
                    break
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0][1]
        for fn, cid in cands:       # disambiguate by first name / nickname prefix
            if fn == first or (len(first) >= 3 and (fn.startswith(first)
                                                    or first.startswith(fn))):
                return cid
        return None                 # ambiguous: refuse rather than guess wrong


# --- LLM inference (one config per workbook shape) --------------------------

_TARGET_DOC = (
    "  - roster_tab: the tab that simply lists athletes (names only, no metrics)\n"
    "  - roster_last_col / roster_first_col: the last- and first-name columns in that tab\n"
    "  - name_candidates: header(s) in the SESSION tabs holding the athlete's name "
    "(rows are athletes), most-likely first\n"
    "  - split_candidates: header(s) for the average split / pace per 500m, most-likely first\n"
    "  - rate_candidates: header(s) for the average stroke rate (may be absent in some tabs)\n"
    "  - watts_candidates: header(s) for average watts/power (may be absent)\n"
)


def _build_prompt(tabs_preview: dict[str, TabPreview]) -> str:
    blocks = "\n\n".join(
        wrap_untrusted(_render_tab(t, p)) for t, p in tabs_preview.items()
    )
    return (
        "You configure an ingest for a PIVOTED rowing-erg workbook. Its shape: "
        "one tab is a ROSTER (a plain list of athlete names). Every OTHER tab is "
        "a single erg TEST SESSION where each ROW is a different athlete and the "
        "columns are that session's split / stroke-rate / watts. Column names "
        "drift between session tabs, so give a ranked list of candidate headers "
        "per field; ingest code picks whichever is present in each tab.\n\n"
        "Emit this config (JSON only, no prose, no code fence):\n" + _TARGET_DOC +
        "\nHere are the workbook tabs with headers and sample rows:\n\n" + blocks +
        '\n\nRespond with ONLY:\n'
        '{"roster_tab": "...", "roster_last_col": "...", "roster_first_col": "...", '
        '"name_candidates": ["..."], "split_candidates": ["..."], '
        '"rate_candidates": ["..."], "watts_candidates": ["..."]}\n'
        "Every header you name must be an exact header that appears above."
    )


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("rowing mapping rejected: LLM did not return JSON (%s)", e)
        raise RowingIngestError("LLM did not return parseable JSON") from e


def validate_mapping(data: Any, tabs_preview: dict[str, TabPreview]) -> RowingMapping:
    if not isinstance(data, dict):
        raise RowingIngestError("mapping output was not a JSON object")
    roster_tab = data.get("roster_tab")
    if roster_tab not in tabs_preview:
        raise RowingIngestError(f"unknown roster_tab {roster_tab!r}")
    roster_headers = set(tabs_preview[roster_tab].headers)
    for field in ("roster_last_col", "roster_first_col"):
        if data.get(field) not in roster_headers:
            raise RowingIngestError(f"{field} {data.get(field)!r} not in roster tab")
    # candidate lists must be non-empty (name+split required) and reference real
    # headers somewhere in the session tabs.
    session_headers: set[str] = set()
    for tab, prev in tabs_preview.items():
        if tab != roster_tab:
            session_headers.update(prev.headers)

    def _clean_list(field: str, *, required: bool) -> tuple[str, ...]:
        raw = data.get(field) or []
        if not isinstance(raw, list):
            raise RowingIngestError(f"{field} must be a list")
        vals = tuple(c for c in raw if isinstance(c, str) and c in session_headers)
        if required and not vals:
            raise RowingIngestError(f"{field}: no candidate matches a session header")
        return vals

    return RowingMapping(
        roster_tab=roster_tab,
        roster_last_col=data["roster_last_col"],
        roster_first_col=data["roster_first_col"],
        name_candidates=_clean_list("name_candidates", required=True),
        split_candidates=_clean_list("split_candidates", required=True),
        rate_candidates=_clean_list("rate_candidates", required=False),
        watts_candidates=_clean_list("watts_candidates", required=False),
    )


def infer_mapping(
    tabs_preview: dict[str, TabPreview], *, llm: Callable[[str], str]
) -> RowingMapping:
    return validate_mapping(_extract_json(llm(_build_prompt(tabs_preview))), tabs_preview)


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower())


def _known_ag_mapping(tabs_preview: dict[str, TabPreview]) -> RowingMapping | None:
    """Deterministic config for AG's current women's rowing workbook shape.

    Keep the LLM fallback for unfamiliar pivoted workbooks, but don't spend an
    API call when the roster/session headers are already the stable AG pattern.
    """
    roster_tab = None
    for tab, prev in tabs_preview.items():
        headers = {_norm_header(h): h for h in prev.headers}
        if "last name" in headers and "first name" in headers:
            roster_tab = tab
            roster_last = headers["last name"]
            roster_first = headers["first name"]
            break
    if roster_tab is None:
        return None

    session_headers: list[str] = []
    for tab, prev in tabs_preview.items():
        if tab != roster_tab:
            session_headers.extend(prev.headers)
    by_norm: dict[str, list[str]] = {}
    for h in session_headers:
        by_norm.setdefault(_norm_header(h), []).append(h)

    def candidates(*names: str) -> tuple[str, ...]:
        out: list[str] = []
        for name in names:
            for h in by_norm.get(name, []):
                if h not in out:
                    out.append(h)
        return tuple(out)

    name_candidates = candidates("name")
    split_candidates = candidates("avg split", "average", "split")
    if not name_candidates or not split_candidates:
        return None
    return RowingMapping(
        roster_tab=roster_tab,
        roster_last_col=roster_last,
        roster_first_col=roster_first,
        name_candidates=name_candidates,
        split_candidates=split_candidates,
        rate_candidates=candidates("avg rate", "rate"),
        watts_candidates=candidates("avg watts", "average watts"),
    )


# --- deterministic apply: one athlete -> Activities -------------------------

def _pick(row: dict, candidates: tuple[str, ...]) -> object:
    for c in candidates:
        if row.get(c) is not None:
            return row.get(c)
    return None


def extract_activities(
    tabs_preview: dict[str, TabPreview],
    read_rows: Callable[[str], list[dict]],
    mapping: RowingMapping,
    roster: RowingRoster,
    *,
    chosen_id: str,
    athlete_id: str,
) -> list[Activity]:
    """Walk every session tab, isolate the chosen athlete's row, and map each
    erg piece to a Sport.OTHER Activity. Row VALUES never went to the LLM."""
    out: list[Activity] = []
    for tab in tabs_preview:
        if tab == mapping.roster_tab:
            continue
        d, label = parse_tab_date(tab)
        if d is None:
            logger.info("rowing: skipping tab %r (undecodable date)", tab)
            continue
        kind, magnitude = parse_piece(label)
        for row in read_rows(tab):
            if roster.resolve(_pick_name(row, mapping)) != chosen_id:
                continue
            split_sec = parse_clock(_pick(row, mapping.split_candidates))
            if split_sec is None or split_sec <= 0:
                break  # the athlete's row has no usable split -> nothing to map
            act = _build_activity(d, label, kind, magnitude, split_sec, row,
                                  mapping, athlete_id)
            if act is not None:
                out.append(act)
            break  # one row per athlete per session
    return out


def _pick_name(row: dict, mapping: RowingMapping) -> object:
    return _pick(row, mapping.name_candidates)


def _build_activity(
    d: date, label: str, kind: str, magnitude: float | None, split_sec: float,
    row: dict, mapping: RowingMapping, athlete_id: str,
) -> Activity | None:
    speed_mps = 500.0 / split_sec
    if kind == "distance_m" and magnitude:
        distance_m, moving_sec = magnitude, magnitude / speed_mps
    elif kind == "duration_s" and magnitude:
        moving_sec, distance_m = magnitude, speed_mps * magnitude
    else:                       # unknown geometry: still record the split signal
        moving_sec, distance_m = 0.0, 0.0
    rate = parse_clock(_pick(row, mapping.rate_candidates))   # bare spm, no colon
    watts = _pick(row, mapping.watts_candidates)
    piece = re.sub(r"\s+", " ", label).strip() or "erg"
    try:
        return Activity(
            activity_id=f"erg-{athlete_id}-{d.isoformat()}-{piece.replace(' ', '_')}",
            source=Source.SHEET,
            athlete_id=athlete_id,
            start_local=datetime.combine(d, time(17, 0)),
            local_date=d,
            name=f"{piece} erg @{_clock(split_sec)}/500m",  # UntrustedText -> fenced/encrypted
            sport=Sport.OTHER,
            moving_time_sec=round(moving_sec, 1),
            distance_mi=round(distance_m / _M_PER_MI, 3),
            avg_speed_mph=round(_split_to_mph(split_sec), 3),
            avg_watts=float(watts) if watts is not None else None,
            avg_cadence=rate,
        )
    except (TypeError, ValueError, ValidationError) as e:
        logger.warning("rowing: dropped %s %s row: %s", d, piece, e)
        return None


# --- encrypted, fingerprinted mapping cache ---------------------------------

def _fingerprint(tabs_preview: dict[str, TabPreview]) -> str:
    import hashlib
    payload = json.dumps({t: tabs_preview[t].headers for t in sorted(tabs_preview)},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(path: Path, key: bytes, fingerprint: str) -> RowingMapping | None:
    if not path.exists():
        return None
    try:
        data = json.loads(crypto.decrypt(path.read_bytes(), key))
    except Exception:
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    m = data["mapping"]
    return RowingMapping(
        roster_tab=m["roster_tab"], roster_last_col=m["roster_last_col"],
        roster_first_col=m["roster_first_col"],
        name_candidates=tuple(m["name_candidates"]),
        split_candidates=tuple(m["split_candidates"]),
        rate_candidates=tuple(m["rate_candidates"]),
        watts_candidates=tuple(m["watts_candidates"]),
    )


def _save_cache(path: Path, key: bytes, fingerprint: str, m: RowingMapping) -> None:
    blob = crypto.encrypt(json.dumps({
        "fingerprint": fingerprint,
        "mapping": {
            "roster_tab": m.roster_tab, "roster_last_col": m.roster_last_col,
            "roster_first_col": m.roster_first_col,
            "name_candidates": list(m.name_candidates),
            "split_candidates": list(m.split_candidates),
            "rate_candidates": list(m.rate_candidates),
            "watts_candidates": list(m.watts_candidates),
        },
    }).encode("utf-8"), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)


def _default_llm(settings) -> Callable[[str], str]:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def call(prompt: str) -> str:
        resp = client.messages.create(
            model=settings.anthropic_model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text")
    return call


def ingest_rowing(
    tabs_preview: dict[str, TabPreview],
    read_rows: Callable[[str], list[dict]],
    *,
    settings,
    key: bytes,
    athlete_query: str,
    athlete_id: str,
    llm: Callable[[str], str] | None = None,
) -> list[Activity]:
    """Resolve (cache or infer) the workbook config, isolate `athlete_query`,
    and return that athlete's erg sessions as Activities."""
    mapping = _resolve_mapping(tabs_preview, settings=settings, key=key, llm=llm)
    roster = RowingRoster.from_rows(
        read_rows(mapping.roster_tab), mapping.roster_last_col, mapping.roster_first_col)
    chosen = roster.resolve(athlete_query)
    if chosen is None:
        raise RowingIngestError(f"athlete {athlete_query!r} is not a single roster athlete")
    acts = extract_activities(tabs_preview, read_rows, mapping, roster,
                              chosen_id=chosen, athlete_id=athlete_id)
    if not acts:
        raise RowingIngestError(f"no erg sessions found for {athlete_query!r}")
    return acts


def _resolve_mapping(
    tabs_preview: dict[str, TabPreview],
    *,
    settings,
    key: bytes,
    llm: Callable[[str], str] | None = None,
) -> RowingMapping:
    fingerprint = _fingerprint(tabs_preview)
    cache_path = Path(settings.synth_token_dir) / "rowing_mapping.enc"
    mapping = _known_ag_mapping(tabs_preview) or _load_cache(cache_path, key, fingerprint)
    if mapping is None:
        mapping = infer_mapping(tabs_preview, llm=llm or _default_llm(settings))
        _save_cache(cache_path, key, fingerprint, mapping)
        logger.info("inferred rowing mapping: roster=%r name=%s split=%s",
                    mapping.roster_tab, mapping.name_candidates, mapping.split_candidates)
    return mapping


def ingest_rowing_roster(
    tabs_preview: dict[str, TabPreview],
    read_rows: Callable[[str], list[dict]],
    *,
    settings,
    key: bytes,
    llm: Callable[[str], str] | None = None,
) -> list[Activity]:
    """Map every roster athlete found in a pivoted rowing workbook."""
    mapping = _resolve_mapping(tabs_preview, settings=settings, key=key, llm=llm)
    roster = RowingRoster.from_rows(
        read_rows(mapping.roster_tab), mapping.roster_last_col, mapping.roster_first_col)
    acts: list[Activity] = []
    for athlete_id in roster.all_athletes():
        acts.extend(extract_activities(tabs_preview, read_rows, mapping, roster,
                                       chosen_id=athlete_id, athlete_id=athlete_id))
    if not acts:
        raise RowingIngestError("no erg sessions found for roster athletes")
    return acts
