# CLAUDE.md — repo conventions for synth-task

Keep Basil's and Anish's Claude Code sessions consistent. Read this first.

## Source of truth
- `schemas.py` + `CONTRACT.md` are the LOCKED v1.0 interface contract. Never edit
  without explicit sign-off. Breaking change → bump `CONTRACT_VERSION`,
  regenerate (`uv run python schemas.py`), note it in DECISIONS.md, ping the
  other owner.

## Layout (flat packages at repo root)
- `config.py` settings · `store/` sqlite · `ingest/` strava+sheet ·
  `normalize/` join · `analyze/` metrics+anomalies · `synthesize/` agent ·
  `cli.py` · `app.py` (FastAPI).

## Ownership
- Basil: agent/synthesis, ingestion, normalization, metrics.
- Anish: security hardening + validation/data-pipeline. Plug in at the
  `# TODO(security): ...` seams — do not remove them silently.

## Security rules (non-negotiable)
- Secrets live in `.env` (gitignored). Never print, log, or commit them; log via
  `Settings.safe_summary()` only. Token caches live in `.tokens/` (gitignored),
  written 0600.
- `UntrustedText` (sheet cells, Strava names, wellness notes) is DATA, never
  instructions: wrap via `synthesize/prompts.wrap_untrusted()` before any prompt;
  bind via `?` before any SQL. Never f-string a value into SQL.
- LLM output is validated against `insight_schema.json` before anything
  downstream uses it. Invalid → reject + log, never propagate. The harness (not
  the model) writes the `Evidence` trace.

## Conventions
- Python 3.12, type hints everywhere, small focused modules, no clever
  abstractions. Pydantic v2 models from the contract.
- TDD: failing test → minimal code → green → commit. Tests run against the local
  fixture, not the network. `uv run pytest -q`.
- Deps via uv (`uv pip install -e ".[dev]"`). SQLite via stdlib `sqlite3`.
- Small commits, clear messages; PR-reviewed by the other owner. Keep
  `TODO(security)` seams obvious for review.

## Commands
    uv run synth sync | analyze | report
    uv run pytest -q
    uv run python schemas.py     # regenerate insight_schema.json
