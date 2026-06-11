"""LLM-output validation. Owner: Anish (security seam).

Contract rule (CLAUDE.md): LLM output is validated against insight_schema.json
before anything downstream uses it; invalid -> reject + log, never propagate.

The harness, not the model, owns the report identity and the Evidence trace, so
those fields are stripped from the model's output and supplied authoritatively
by the caller. This is the last line of defense against a successful prompt
injection: even if a malicious sheet note steers the model, it still cannot
forge a tool-call trace, set off-contract fields, or smuggle extra keys
downstream — anything off-contract is rejected here.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from schemas import SynthesisReport

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "insight_schema.json"

# Fields the harness fills authoritatively; the model must never author them.
# `evidence` is the tool-call trace shown to AG — forging it is the highest-value
# injection target, so it is always taken from the harness, never the model.
HARNESS_OWNED_FIELDS = frozenset({
    "contract_version", "report_id", "generated_at", "data_coverage", "evidence",
})


class InsightRejected(Exception):
    """LLM output failed contract validation. Callers must NOT propagate the output."""


@lru_cache(maxsize=1)
def _schema() -> dict:
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


def validate_insight(
    model_output: str | dict, *, harness_fields: dict[str, Any] | None = None
) -> SynthesisReport:
    """Validate raw LLM output and return a typed SynthesisReport, or reject it.

    `model_output` is the model's JSON (str or already-parsed dict).
    `harness_fields` are the harness-owned values (report_id, generated_at,
    evidence, ...) merged in authoritatively after stripping any the model tried
    to set itself. Raises InsightRejected on anything off-contract.
    """
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError as e:
            logger.warning("LLM output rejected: not valid JSON (%s)", e)
            raise InsightRejected("LLM output was not valid JSON") from e
    if not isinstance(model_output, dict):
        logger.warning(
            "LLM output rejected: top-level value is %s, not an object",
            type(model_output).__name__,
        )
        raise InsightRejected("LLM output was not a JSON object")

    # Drop any harness-owned fields the model tried to author (e.g. a forged
    # Evidence trace) — a red flag worth logging — then let the harness win.
    forged = HARNESS_OWNED_FIELDS & model_output.keys()
    if forged:
        logger.warning(
            "LLM tried to set harness-owned field(s) %s; dropping them", sorted(forged)
        )
    candidate: dict[str, Any] = {
        k: v for k, v in model_output.items() if k not in HARNESS_OWNED_FIELDS
    }
    candidate.update(harness_fields or {})

    try:
        # FormatChecker enforces the date / date-time formats too, not just shape.
        jsonschema.validate(candidate, _schema(), format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as e:
        # Log the location and a short reason only — the payload may carry
        # injected or PII content we don't want in logs.
        logger.warning(
            "LLM output rejected: schema violation at %s: %s",
            list(e.absolute_path), e.message[:200],
        )
        raise InsightRejected(f"schema violation at {list(e.absolute_path)}") from e

    # Pydantic re-validation: defense in depth, and yields the typed object.
    try:
        return SynthesisReport.model_validate(candidate)
    except Exception as e:  # pydantic ValidationError and friends
        logger.warning(
            "LLM output rejected: failed model validation (%s)", type(e).__name__
        )
        raise InsightRejected("failed pydantic validation") from e
