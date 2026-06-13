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

## Prompt-injection defense lives at the prompt boundary, not the parser
The only attacker-controllable input is the manually-entered sheet (wellness
`notes` is the contract's PRIMARY injection surface; Strava names/device too).
The defense is NOT to sanitize at parse time — a real note can legitimately read
"ignore the pain, pushed through", and stripping it would corrupt the analysis.
Untrusted text is captured faithfully (encrypted at rest) and neutralized at the
two LLM boundaries:
- **`synthesize/prompts.wrap_untrusted()`** (seam #1) fences untrusted text with
  a 128-bit per-call nonce (`<untrusted_data:nonce>…`), so a payload cannot forge
  the closing tag and break out, plus a "this is data, not instructions" preamble.
- **`synthesize/validate.validate_insight()`** (seam #2) validates LLM output
  against `insight_schema.json` (jsonschema + FormatChecker, then pydantic) before
  anything downstream uses it; invalid → reject + log, never propagate. Harness-
  owned fields (`report_id`, `generated_at`, `data_coverage`, and especially the
  `evidence` tool-call trace — "filled by harness, not the LLM") are stripped from
  the model output and supplied authoritatively by the caller, so a steered model
  cannot forge a trace or smuggle off-contract/extra keys through.
Both are pure, tested primitives; Basil's `synthesize/` agent wires them in (wrap
every `UntrustedText` into prompts; route every model response through validate).

## Wellness ingestion: LLM infers the column mapping, code does the parsing
Real workbooks don't share a layout — AG's export keeps wellness in
`daily_summary` under names like `date`/`in_bed`/`notes`, not the empty
`health_raw` the original parser hard-coded, so zero wellness ingested.
`ingest/mapping.py` fixes this by inferring a column→`WellnessDay` mapping ONCE
per workbook shape, then parsing deterministically:
- **The LLM is a config compiler, not a runtime DB agent.** It sees only column
  headers + a few sample cells (wrapped via `wrap_untrusted`), never full row
  values; those stay in deterministic code. Its output is strictly validated
  (known target fields only, sources must be real columns, `local_date`
  required) before use — invalid → reject + log.
- **Canonical fast-path:** sheets already using contract field names map by
  identity with no LLM call (keeps conformant sheets + test fixtures offline);
  the LLM is only the fallback for non-conforming layouts.
- **Empty tabs aren't offered** as candidates (skips the empty `health_raw`).
- The mapping is **cached encrypted**, keyed by a header fingerprint, so cost
  scales per sheet-shape, not per row or per sync.
- `notes` remains the encrypted-at-rest injection surface via the existing seam.
Per-activity splits (`run_splits_raw`/`bike_splits_raw`/`swim_splits_raw`) ARE
now ingested into `run_split`/`bike_split`/`swim_split` (PK
`(activity_id, split_index)`) for the agent's drill-down; `SwimSplit.stroke_style`
(UntrustedText) is encrypted at rest like other PII. Rows without an
`activity_id` are skipped as padding (verified to carry no split metrics).
`run_segments_raw` is NOT ingested: the contract has no `RunSegment` model, so
capturing it needs a `CONTRACT_VERSION` bump + sign-off — deferred as a separate
proposal.

## Real-data fixture stays private
`triathlon_sheet.xlsx` and the loose `*.csv` export are real personal training
data. They are gitignored. Before submission we either keep the repo PRIVATE or
anonymize the fixture. Flagging here so the call is explicit, not accidental.

## Sheet ingest is row-oriented; the daily join is computed, not stored
`ingest/sheet.py` parsers take rows (list of dicts) — the file format lives in
two thin loaders (stdlib csv for tab exports, openpyxl for the original xlsx
workbook the take-home shipped as). Both yield identical str|None dicts, so
parsing/validation is format-agnostic and tested once. `DailyRow` is produced
by the pure function `normalize/join.build_daily_rows` on demand and never
materialized: at this scale recomputation is instant, and a stored copy would
need invalidation on every re-sync. Revisit only if `analyze/` proves it needs
SQL over days. Wellness rows land in a `wellness` table with `notes` (the
contract's primary injection surface) encrypted like the activity PII columns.
Wellness column names are an assumption until AG populates the tab
(CONTRACT.md open items 1–2). `synth sync` now syncs every *configured* source
and skips unconfigured ones instead of crashing. Export rows with no
activity_id (real watch-app rows, 33/375 in the local file) get a
deterministic `sheet-<start>-<sport>` fallback id so they ingest idempotently
instead of failing the whole sync.

## Foundation commit for pre-existing contract/config files
The plan assumed `schemas.py`, `CONTRACT.md`, `.gitignore`, `.env.example`,
`config.py`, and `uv.lock` "already existed", but no task committed them. They
were committed together early (before any secret could land) so `.gitignore`
protects `.env`/`.tokens/`/`*.db` from the first moment and the locked contract
is under version control.

## Analyze: padded-calendar windows, split-half trends, deterministic anomaly ids
`analyze/metrics.py` computes everything relative to the athlete's OWN rolling
history, per the spec's detector catalog. The non-obvious calls:
- **Calendar padding.** DailyRows only exist for days with data; windows run
  over the full calendar span, where a missing day = 0 training minutes and a
  `rest_day=True` DailyMetrics. Rest days are signal, and skipping them would
  inflate every rolling load.
- **Gating:** acute needs ≥7 calendar days, chronic/ACWR/z-score ≥28
  (`None` below — spec rule), z-score also `None` at zero variance, ACWR `None`
  at zero chronic. Population std.
- **Trends are split-half** (recent 7d mean vs prior 7d mean, ≥2 valued days
  per half) rather than regression: trivially explainable in an anomaly
  description and to the agent. HR-at-pace uses **beats per mile**
  (`avg_hr × pace`) as the decoupling proxy.
- **Thresholds** (tunable constants at the top of the module): ACWR safe window
  0.8–1.3, watch outside it, flag >1.5 (Gabbett); load z>2 watch / z>3 flag
  (high side only — the low side is ACWR<0.8's job); trends >5% watch / >10%
  flag; rhr/hrv z ±2 watch / ±3 flag against a 28d rolling baseline gated on
  ≥14 values. The wellness detectors are LIVE, not dormant — the real
  workbook's daily_summary populates rhr/hrv.
- **Deterministic anomaly ids** (`athlete:date:metric`) make `synth analyze`
  idempotent via upsert. The locked `Anomaly` model has no athlete_id field;
  the id carries it (single-athlete MVP).
- `daily_metrics`/`anomaly` tables hold only code-computed numerics and
  code-authored descriptions (trusted per contract) — no encrypted columns, so
  the agent's queries can filter on them.

## Synthesis: harness-brokered tool loop, model authors only the narrative
`synthesize/agent.py` runs an Anthropic tool loop over four read-only tools
(`synthesize/tools.py`: `query_anomalies`, `get_daily_metrics`,
`get_activity_detail`, `compare_periods`). The non-obvious calls:
- **The harness, not the model, owns the trace and identity.** Every tool call
  is brokered by the loop, which appends an `Evidence` row (`Evidence.tool` is a
  closed Literal of the four names) and fills `report_id`/`generated_at`/
  `contract_version`/`data_coverage`. These are passed to `validate_insight()`
  as `harness_fields`, which strips any the model tried to author. Result: a
  hijacked model cannot forge a tool it never called or fake the report identity
  (tested in test_agent_loop.py).
- **Tools are pure functions over the store**, split into `tools.py` so they run
  with no model/network — the loop and tools are tested entirely offline via an
  injected fake client. The real `anthropic.Anthropic` client is only
  constructed when `client is None`.
- **UntrustedText is fenced at the tool boundary** (`get_activity_detail` wraps
  activity name/device and swim stroke via `wrap_untrusted`) because a tool
  result is fed straight back to the model as content. We fence, never censor.
- **Bounded + fail-closed:** <=12 model turns; unknown tool name -> error
  tool-result and no Evidence row; malformed/off-contract final JSON or an
  exhausted loop -> `InsightRejected` (reject + log, never propagate).
- CLI `report` wiring and the FastAPI surface are a separate follow-on; this
  plan delivers `run_synthesis()` as the callable seam.
