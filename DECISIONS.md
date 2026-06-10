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
between runs and surfaces `?error=` denials explicitly.

## Token encryption at rest: AES-256-GCM, per-machine auto-generated key
The refresh token is now encrypted at rest (`security/crypto.py`), filling the
`# TODO(security)` seam in `ingest/strava.py`. Cipher is **AES-256-GCM**
(authenticated: a wrong key or any tampering fails loudly with `InvalidTag`,
never silent garbage) — *not* a hash like SHA-256, which is one-way and can't be
decrypted back into a usable token. A random 12-byte nonce is prepended per
write. The 32-byte key is **auto-generated on first run** into
`.tokens/synth.key` (0600, gitignored) and is **per-machine**: never committed,
never transported between collaborators. That's the right model because the data
it protects — the per-account token cache — is itself per-machine, so there is
no shared secret to manage. Threat model: this defends against the token leaking
*off* the box (accidental commit, backup/sync of `.tokens/`, a copied repo); it
does NOT defend against an attacker with full read access to the home dir, who
gets key + ciphertext together. Raising that bar (OS keyring / passphrase) is a
later swap behind the same `crypto.encrypt/decrypt` interface.

## DB at rest: field-level encryption of PII columns, not whole-file
The `store/db.py` seam is filled with **field-level** AES-256-GCM encryption of
the `UntrustedText` free-text columns (`name`, `device_name` — the PII /
injection surface), reusing `security/crypto.py` and the same per-machine key.
Whole-file encryption (SQLCipher) was rejected: it needs a non-stdlib driver
(`pysqlcipher3`), which contradicts the stdlib-`sqlite3` decision above. Numeric
metrics are left plaintext on purpose so the agent's tools can still filter and
the `(athlete_id, local_date)` index stays useful. Encrypted cells carry an
`enc:v1:` prefix over base64(nonce||ciphertext||tag); the prefix lets reads pass
plaintext/legacy rows through untouched, so the column can be migrated in place.
Encryption is keyed (`upsert_activities(..., key=)` / `get_activities(..., key=)`);
`sync_strava` always supplies the key, so the live path is encrypted by default.

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
