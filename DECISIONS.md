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

## Serving layer: one generate_report seam behind both the CLI and the API
`synth report` and FastAPI `/insights` are thin wrappers over
`synthesize/report.generate_report()`, which resolves the target and calls
`run_synthesis`. The non-obvious calls:
- **Target resolved from data, not config.** `resolve_target` defaults the
  athlete to whoever has the most `daily_metrics` rows and the period to that
  athlete's full span, so `synth report` with no flags works against whatever
  was ingested (workbook `ag` vs Strava `basil`). Flags override; no metrics ->
  ValueError -> clean CLI message / HTTP 404.
- **CLI keeps stdout pure JSON:** the report prints to stdout, the redacted
  config + status to stderr, so `synth report | jq` works.
- **API is fail-closed:** `/insights` maps missing data -> 404 and a rejected
  model output -> 502 with a generic detail, never echoing the rejected payload
  (which could carry injected/PII content). `/sync` mirrors the CLI's
  source-skipping; `/health` is static.
- Endpoints/commands import the wrapped functions by name so tests monkeypatch
  them and never touch the network — the agent's injected-client seam keeps the
  whole serving layer offline-testable.

## Agent loop hardened against real model output (live smoke)
The offline scripted tests passed but the first live run (claude-opus-4-8 over
the real workbook) exposed three gaps, now fixed + regression-tested: the model
prepends prose around a ```json fence (robust `_extract_json`), a report citing
75 anomaly_ids overran 4096 output tokens (raised to 16384), and the model
invented `Pattern` keys because the system prompt never described the `Pattern`
shape (prompt now lists every contract field + enum; a test asserts the prompt
covers `Pattern.model_fields`). Lesson: validate_insight's fail-closed rejection
worked exactly as designed — but agent UX needs at least one real-model run, not
just scripted fakes.

## Strava + sheet are ONE athlete, two sources (not two athletes)
Earlier the pipeline stamped Strava under `strava_athlete_id` ("anish") and the
sheet under a hard-coded "ag", treating them as two athletes kept distinct by
`athlete_id`. That was wrong: the product's whole point is fusing ONE athlete's
training (Strava) and recovery/wellness (sheet) into a single picture. With split
ids the daily join (`(athlete_id, local_date)`) never fused, so a report saw only
one silo (Strava activities with `n_wellness_days=0`, or sheet data alone). Fix:
both ingest paths stamp the same `athlete_id` (Strava's configured id);
provenance still lives on the `source` axis (STRAVA_API vs SHEET) and survives to
synthesis via `DailyRow.source_mix`, so nothing is lost by sharing the id.
Existing split-id rows are reproducible, so they get remapped/re-synced rather
than migrated carefully.

## Synthesis voice: physiology-literate coach, insight over summary, evidence last
The synthesis system prompt was a neutral "analyst" that produced exhaustive,
jargon-heavy recaps (ACWR, aerobic decoupling, z-scores) the athlete couldn't
read. Reframed it to reason like an endurance coach/exercise physiologist for ONE
athlete: surface only the few patterns that change how they train/recover (2-4,
not a full enumeration), distinguish real signal from statistical artifact (e.g.
cold-start ratio spikes off a near-zero base), and speak in plain athlete language
— lead with the takeaway and what to do, cite the numbers/metric-names as
supporting EVIDENCE at the end (technical names live in `metrics_involved`). The
locked output schema is unchanged; this is prompt/voice only, so validate_insight
and the contract still hold.

## Synthetic Strava test harness for non-obvious-insight validation
Anish's real Strava history is too sparse to exercise the synthesis agent, so
`scripts/gen_test_strava.py` generates a deterministic (seed=42) synthetic
athlete — ~5 months (Dec 2025-May 2026) of run/bike/swim activities + daily
wellness — into a SEPARATE `synth_test.db` (via `SYNTH_DB_PATH`), never touching
the real `synth.db`. Value ranges are calibrated from the real workbook
(`activities_raw`). It plants three deliberately NON-OBVIOUS patterns, each
tuned to `analyze/metrics.py` thresholds, to test whether the coach-agent
surfaces what no single day reveals:
- (A) masked aerobic decoupling — run pace held flat while HR creeps up, so
  HR-at-pace drifts up with NO single anomaly firing (ACWR stays ~1.05);
- (B) recovery markers lead — HRV-suppressed / RHR-elevated anomalies fire ~2
  weeks BEFORE the HR-at-pace drift, discoverable only by fusing both sources;
- (C) the ACWR paradox — the low-ACWR April week (detraining "watch") is the
  HEALTHY recovery, while the normal-looking ACWR hid the March overreach.
Validated 2026-06-13: the agent found all three (plus correctly inferred the
root cause — a 10+ week build with no deload — and dismissed the planted
noise). The `.db` artifact stays gitignored (`*.db`); only the generator is
tracked so the fixture is reproducible, not committed as data.

## Flexible AI-fallback ingest for a dissimilar, multi-athlete workbook
AG supplied a second real workbook (`rowing_women_2025-2026 ERGS-2.xlsx`) shaped
NOTHING like our triathlon export — it is PIVOTED: one tab per erg TEST SESSION
(date encoded in the tab name, e.g. `316 2k` = Mar 16), each ROW a different
athlete (~40 women), with column layouts drifting tab to tab and no wellness
data at all. AG's ask: ingest it, isolate ONE athlete, simulate Strava, and see
if the agent finds patterns. Two new data-pipeline capabilities, both built to
stay inside the LOCKED contract:
- `ingest/rowing.py` — the general-schema sibling of `ingest/mapping.py`. An LLM
  infers a mapping CONFIG once per workbook shape (roster tab, name column,
  ranked per-field header candidates for split/rate/watts), validated before
  use and cached encrypted by header fingerprint (`.tokens/rowing_mapping.enc`,
  0600). Deterministic code then parses tab-name dates, piece geometry
  (`2x6k`->12000 m, `3x12`->2160 s), and maps each erg piece to a `Sport.OTHER`
  Activity (per-500m split rides in `avg_speed_mph`; stroke rate->`avg_cadence`).
  Only headers + sample cells go to the LLM (wrapped via `wrap_untrusted`), never
  full row values. Live run: the LLM inferred every header variant correctly.
- Athlete identity: `RowingRoster.resolve()` canonicalises dirty session names
  against the roster — trailing spaces, nicknames (`Cox, Maddy`->`cox-madeline`),
  truncated hyphenated surnames (`Wappler-N`->`Wappler-Niemeyer`) — and REFUSES
  names not on the roster (the "is/ isn't a single athlete" requirement).
- `analyze/rowing.py` (`detect_erg_anomalies`) — an ADDITIVE detector kept out of
  Basil's locked `metrics.py`, wired into `cli analyze` behind a seam comment. It
  emits standard `Anomaly` rows (the `metric` field is a free string per the
  contract, so NO schema bump): `erg_split_regression` / `erg_split_plateau`,
  trended WITHIN each piece family (a 2k max effort ~1:45/500m is not comparable
  to a 6k ~1:58/500m). Generic load/ACWR/z-score already work sport-agnostically;
  run-specific pace/HR-at-pace trends correctly stay null for erg.
- `scripts/gen_rowing_test.py` — single harness: ingests Banks, Claire's 16 erg
  sessions via the AI fallback AND lays down deterministic (seed=42) simulated
  cross-training + wellness (Sep 2025-Mar 2026) into a separate `rowing_test.db`.
  Plants a late-Jan->Mar overload (load ramp + suppressed HRV + elevated RHR)
  that lines up with her erg plateau (no 2x6k PR after Feb 9). Validated
  2026-06-14: the agent fused both sources and read it as non-functional
  overreaching ("training more but no longer getting faster — digging a hole, not
  building"), isolated the one athlete, and dismissed a planted Oct-10 noise day.

## Workbook layout: explicit SHEET_KIND (auto-detect as fallback); readable report
- The source SHAPE is an EXPLICIT setting: `SHEET_KIND=tri|rowing` (config
  `sheet_kind`, a `Literal` so a bad value is rejected at load). It is
  authoritative; `sync_sheet` routes on it via `_resolve_kind`. We chose explicit
  over pure auto-detection because a header heuristic can silently misroute an
  unseen workbook. When `SHEET_KIND` is UNSET, sync falls back to header-based
  `ingest/sheet.py::detect_layout` (triathlon `activities_raw`-style vs pivoted
  rowing: roster tab + name-keyed session tabs; defaults to `tri`). Rowing needs
  `SHEET_ATHLETE_QUERY` (the roster name to isolate); its rows stamp
  `STRAVA_ATHLETE_ID` (one athlete, two sources). So `synth sync` handles both
  formats directly — the rowing path is no longer harness-only.
- `synthesize/render.py` turns a validated SynthesisReport into a coach-style
  Markdown briefing (takeaway split from `> Evidence:`/`> Worth noting:`, follow-up
  questions, and the harness-written audit trail). `synth report` now prints this
  by default; `--format json` keeps the machine deliverable for pipelines. The
  renderer consumes the already-validated report, so it inherits the same
  guarantees (no forged harness fields, no secrets, untrusted text neutralised).

## Whole-roster trends: per-athlete LLM-planted Strava, one committed test DB
AG's follow-up — "extract individual athlete trends … find different patterns for
each athlete" — runs the SAME pipeline across every rostered athlete instead of
one. We have real erg results per athlete but no Strava for them, so the harness
`scripts/multi_athlete.py` generates a LEAN slice of fake Strava per athlete whose
shape is planted FROM that athlete's own erg trajectory. Decisions:
- The app core is UNCHANGED — no edits to `cli.py`/`app.py`/agent/`metrics.py`.
  The harness drives `ingest/rowing`, `normalize/join`, `analyze/*`, `store/db`.
  The only app-module touch is an additive read-only `RowingRoster.all_athletes()`.
- `classify_trend` reads each athlete's most-tested erg piece family (2x6k, 7
  tests) over the FULL season -> adapting / plateau / overreaching.
- Pattern is PLANTED BY AN LLM, reusing the repo's "LLM as config compiler"
  stance (cf. `ingest/mapping.py`): the model sees ONLY the computed trend numbers
  (no names, no untrusted free text) and emits a small `PatternConfig` (window,
  overload onset, severity). `validate_pattern` bounds-checks it (lean 7-14wk
  window, physiological caps, no overload for adapting); invalid -> reject+log and
  fall back to a deterministic `default_pattern`, so one bad response can't abort a
  47-athlete build. Deterministic `simulate` then EXPANDS the validated config
  into daily rows (RNG only adds per-athlete jitter). The LLM narrative is never
  written to the DB. Configs cached encrypted per trend-fingerprint in `.tokens/`.
- LEAN, not full-season: each athlete gets ~7-14 weeks of Strava (enough for a 28d
  chronic baseline + an overload block to register), and we ingest only the erg
  tests INSIDE that window. Mixing far-back autumn erg tests with a short recent
  Strava block left a near-empty chronic baseline that spiked ACWR for everyone
  and masked the planted per-athlete differences — restricting to the window fixed
  it (clean separation: adapting ~0-5 training anomalies, plateau ~20-30,
  overreaching ~26-42).
- The combined `athletes_test.db` is COMMITTED so AG can run `synth report
  --athlete <id>` with no Strava API and no sheet re-ingest. It is written
  PLAINTEXT (`key=None`) on purpose: the at-rest encryption key is per-machine
  (see [[encryption-key-preference]] / `security/crypto.py`), so an encrypted DB
  couldn't be opened on another checkout. The data is synthetic Strava + real erg
  names already authorized for commit; production `synth sync` still passes a real
  `key=` and encrypts. `.gitignore` un-ignores both real workbooks and this one DB
  via explicit `!` exceptions — AG authorized committing the two workbooks, which
  reverses the earlier repo-privacy default for these two files only.

## Agent anomaly worklist scoped per-athlete (multi-athlete-DB fix)

- Fresh-clone testing surfaced a blocker: `synth report --athlete <id>` against the
  committed 47-athlete `athletes_test.db` failed with `prompt too long: 216k > 200k
  tokens`. Root cause: `synthesize/agent.py` seeded the prompt from
  `query_anomalies(conn)` filtered by DATE only, never by athlete, and the `anomaly`
  table has no `athlete_id` column — the contract carries the athlete in the
  `anomaly_id` prefix (`<athlete_id>:<date>:<metric>`). On the original
  one-athlete-per-DB design this was correct; the combined multi-athlete DB made
  every athlete's worklist leak into one report (e.g. cox-madeline's own 3 anomalies
  vs the 1,105 squad-wide anomalies in her date window).
- Fix (Basil's files — `synthesize/tools.py` + `agent.py`, flagged for PR review):
  `query_anomalies` now takes `athlete_id` and scopes by the `anomaly_id` prefix.
  Correct for BOTH DB shapes — single-athlete DBs share the one prefix, so it's a
  no-op there; combined DBs get only the requested athlete's worklist. No contract
  change (the prefix scheme already encodes the athlete). Regression test:
  `tests/test_agent_tools.py::test_query_anomalies_scopes_to_one_athlete`. Verified
  end-to-end: all three README report targets now run, each scoped to its own
  anomalies (cox-madeline 3, bonnem-lily watch 27, bosio-giulia 41).

## Web frontend (feature/web-frontend)
A coach views reports in a browser. One self-contained static page is served from
`GET /` via `HTMLResponse` (CSS+JS inlined, marked.js from CDN) — no StaticFiles
mount, no second origin, no CORS. `app.py` stays delegation-only; the page lives
in `web.py`. `GET /insights` now also returns `briefing_md`, the human-readable
briefing produced by `synthesize/render.render_markdown` (the task brief called it
`render_report`, but the real function is `render_markdown` — used the real one).
The 404/502 handling is unchanged and rejected payloads are still never echoed.
Per-IP rate limiting (10 req/60s, HTTP 429) is a dependency-free in-process
sliding window: `slowapi` turned out not to be in the lockfile and Redis is
disallowed, so a shared store was avoided. Flagged `# TODO(security)` that the
limiter state is per-process — behind multiple uvicorn workers the effective
limit is (n_workers × 10), so it needs a shared store before any scaled-out
deploy. No contract change; `schemas.py`/`CONTRACT.md` untouched.

## Web frontend, part 2: two selectable datasets + data-driven date pickers
The coach can now analyze EITHER dataset from the form: the rowing squad
(`athletes_test.db`, the committed 47-athlete fixture) and the triathlon athlete
(`tri_test.db`). The browser sends a dataset NAME; `app.DATASETS` is a fixed
name→path allowlist resolved under the repo root — the security boundary that
stops a client steering the app at an arbitrary file (the `dataset=../…` case
returns 404, verified). Unknown name → 404; flagged `# TODO(security)` for the
day datasets become user-supplied. A new read-only, un-rate-limited
`GET /athletes?dataset=` returns each athlete's `[start, end]` span (via the new
`db.athlete_spans`), so the form fills the athlete picker from real data and
**pre-fills + clamps** the date inputs to each athlete's coverage (coach narrows
within, never outside). No contract change.

`tri_test.db` stays gitignored (repo convention: generated DBs are throwaway)
and is reproducible offline from the committed workbook — the triathlon athlete
is stamped `triathlon` via a new backward-compatible `SYNTH_TEST_ATHLETE` env on
`scripts/gen_test_strava.py` (default still `anish`):

    SYNTH_TEST_ATHLETE=triathlon uv run python scripts/gen_test_strava.py tri_test.db
    SYNTH_DB_PATH=tri_test.db SHEET_KIND=tri \
      SHEET_ACTIVITIES_PATH="Copy of Triathlon Training Sync.xlsx" \
      STRAVA_ATHLETE_ID=triathlon STRAVA_CLIENT_ID= STRAVA_CLIENT_SECRET= \
      uv run synth sync
    SYNTH_DB_PATH=tri_test.db uv run synth analyze

Encryption modes can differ per file (rowing fixture is plaintext, `tri_test.db`
is key-encrypted) and still read correctly: `db._decrypt_field` passes
unprefixed plaintext straight through, so a DB is self-consistent as long as the
app reads it with the per-machine key. The two datasets stay in SEPARATE
immutable DBs — no merging — which also avoids mutating the committed fixture.

## Web frontend, part 3: sanitize the rendered briefing (DOM XSS)
The primary render path inserted `marked.parse(briefing_md)` straight into
`innerHTML`. `marked` does not sanitize HTML, and the briefing is built from LLM
output — which, via prompt-injection through `UntrustedText` (Strava names, sheet
cells, wellness notes), can be attacker-influenced. So a model that emitted
`<img src=x onerror=…>`/`<script>` would have executed it in the coach's browser.
Fixed by scrubbing the parsed output with DOMPurify before it touches the DOM
(`DOMPurify.sanitize(marked.parse(md))`); the JSON fallback path already escaped
via `escapeHtml`, so both render paths are now safe. The `wrap_untrusted` fence
protects the prompt, not the render — these are separate boundaries. Both CDN
scripts (`marked`, `dompurify`) are now SRI-pinned (`integrity="sha384-…"`) to
close the supply-chain gap, and the fix fails closed: if DOMPurify can't load,
`renderMarkdown` throws and nothing renders rather than falling back to raw HTML.
No contract change; `schemas.py`/`CONTRACT.md` untouched.
