"""FastAPI wrapper — thin HTTP surface over the same functions the CLI calls.

Placeholder for now: the real /sync /insights /health endpoints land in the
follow-on plan (sheet + join + metrics + agent). Kept as an importable module so
the packaged project (pyproject py-modules includes "app") installs from a clean
clone. Do not add logic here yet — wire endpoints in the follow-on plan.
"""

from __future__ import annotations

# TODO(follow-on): build the FastAPI app:
#   from fastapi import FastAPI
#   app = FastAPI(title="synth")
#   @app.get("/health") ...; @app.post("/sync") ...; @app.get("/insights") ...
# Endpoints must be a thin wrapper over cli/ingest/analyze/synthesize functions.
