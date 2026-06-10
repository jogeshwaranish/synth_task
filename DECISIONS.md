# DECISIONS.md

One paragraph per tradeoff, newest last.

## Storage: stdlib sqlite3 over SQLAlchemy
The store is two grains and a handful of tables. stdlib `sqlite3` with explicit
`?` binds keeps the dependency surface minimal and — more importantly — keeps the
SQL-injection / parameterization security seam visible for Anish's review.
SQLAlchemy's escaping would hide exactly the boundary we want to showcase.

## Layout: flat top-level packages over src/synth
CONTRACT.md refers to bare module paths (`analyze/metrics.py`,
`synthesize/prompts.py`) and the contract is imported as `from schemas import`.
A flat layout honors those paths and keeps imports trivial; a `src/` package
would add prefixes everywhere for no local-run benefit.

## Dependency manager: uv
Single fast tool with a lockfile; `uv run` gives reproducible local execution.
pip-tools would also work but is two tools (compile + sync) for no gain here.

## Strava local_date from start_date_local, not UTC
Strava returns `start_date_local` as wall-clock time with a misleading `Z`
suffix. We strip the `Z`, treat it as naive local, and derive `local_date` from
it — so an 11:58 PM workout stays on the day the athlete trained, per the
contract join rule. `start_date` is kept as the true UTC instant.

## Token cache in .tokens/ (gitignored), written 0600, refresh rotated
Strava rotates the refresh token on every refresh, so we always persist the
returned token. It is a long-lived secret: stored outside git, owner-only
permissions, written atomically via `os.open(..., 0o600)`. The OAuth redirect is
caught by a one-shot localhost HTTP server that resets its captured-code state
between runs and surfaces `?error=` denials explicitly. `# TODO(security)` marks
where Anish's at-rest encryption wraps the token write.

## Real-data fixture stays private
`triathlon_sheet.xlsx` and the loose `*.csv` export are real personal training
data. They are gitignored. Before submission we either keep the repo PRIVATE or
anonymize the fixture. Flagging here so the call is explicit, not accidental.

## Foundation commit for pre-existing contract/config files
The plan assumed `schemas.py`, `CONTRACT.md`, `.gitignore`, `.env.example`,
`config.py`, and `uv.lock` "already existed", but no task committed them. They
were committed together early (before any secret could land) so `.gitignore`
protects `.env`/`.tokens/`/`*.db` from the first moment and the locked contract
is under version control.
