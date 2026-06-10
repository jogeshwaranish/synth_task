# synth-task

Local backend for the synth MVP: pulls Strava + the founder's Google Sheet,
normalizes into the v1.0 contract (`schemas.py`), stores in SQLite at two grains,
computes training-load metrics + anomalies, and runs an Anthropic agent that
emits a `SynthesisReport`.

## Setup
    uv venv --python 3.12 && uv pip install -e ".[dev]"
    cp .env.example .env   # fill in STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET

## Usage
    uv run synth sync       # pull Strava (+ sheet, later) into synth.db
    uv run synth analyze    # compute metrics + anomalies (later)
    uv run synth report     # run the synthesis agent (later)

See `docs/superpowers/specs/` for the design and `DECISIONS.md` for tradeoffs.
