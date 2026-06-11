# Design: Sheet ingest + DailyRow join

**Date:** 2026-06-10 · **Owner:** Basil · **Status:** approved by Basil (this doc)

Fills the next two gaps on the path to the insight MVP: ingesting AG's
training-sheet export into the store, and the per-day join that produces
`DailyRow` — the input `analyze/` needs. Contract shapes (`Activity`,
`WellnessDay`, `DailyRow`) are LOCKED v1.0; this work only produces them,
never modifies them.

## Scope

- `ingest/sheet.py`: parse the sheet export (CSV tab-exports **and** the
  original xlsx workbook) into `Activity` and `WellnessDay`.
- `store/db.py`: a `wellness` table beside `activity`, same encryption seam.
- `normalize/join.py`: pure-function join producing `list[DailyRow]`.
- `config.py` + `cli.py`: sheet paths in settings; `synth sync` syncs whichever
  sources are configured.

Out of scope: splits tabs (`RunSplit` etc.), live Google Sheets API, `analyze/`
metrics, any materialized daily table.

## Architecture

```
CSV / xlsx ──load rows──> parse_*_rows() ──> Activity / WellnessDay
                                                   │ upsert (PII encrypted)
Strava API ── sync_strava (existing) ──────────────┤
                                                   ▼
                                              SQLite store
                                                   │ read
                                                   ▼
                              build_daily_rows() ──> list[DailyRow]   (pure, on demand)
```

Decisions and the reasoning behind them:

1. **Join is a pure function, not a table.** At this scale (~374 activities)
   recomputing is instant; no cache invalidation on re-sync; trivially
   testable. If `analyze/` later proves it needs persistence, a table is easy
   to add then (YAGNI now).
2. **Sheet data flows through the same store.** One `activity` table,
   distinguished by `source` (`Source.SHEET` and sheet IDs like "A0001" are
   already in the locked contract). The agent's drill-down tools will read the
   store, so bypassing it is a dead end.
3. **Parsers take rows (list of dicts), not file paths.** Format is isolated
   to two thin loaders — `_rows_from_csv(path)` (stdlib `csv`) and
   `_rows_from_xlsx(path, tab)` (openpyxl, already a dependency). The take-home
   was distributed as an xlsx; Basil's local copy is a per-tab CSV export. Both
   must work; `sync_sheet` picks the loader by extension.
4. **Wellness parser is built now** even though AG's tabs are empty as of
   June 9 — the contract says late-arriving wellness is the normal case.

## Components

### `ingest/sheet.py`

- `parse_activity_rows(rows, *, athlete_id="ag") -> list[Activity]`
  - The export already carries converted units (`distance_mi`,
    `total_elevation_gain_ft`, `average_speed_mph`) — use them directly, no
    unit math.
  - Quirks: `start_date_local` is a naive space-separated wall-clock string
    ("2026-05-14 4:05:28") → `start_local`, and `local_date` derives from it
    (join rule: never UTC). `start_date_utc` is ISO-with-Z → `start_utc`.
    `TRUE`/`FALSE` strings → bool. Blank cells → `None`. Unknown sports →
    `Sport.normalize` → `OTHER`.
  - `source=Source.SHEET`; `name`/`device_name` are `UntrustedText` (data,
    never instructions).
- `parse_wellness_rows(rows, *, athlete_id="ag") -> list[WellnessDay]`
  - Expected columns mirror the schema: `local_date, in_bed_hours,
    asleep_hours, snoring, rhr, hrv, body_weight_lb, sauna_mins, notes`.
  - **Documented assumption**: the real tab is empty, so these names are a
    guess to verify when AG populates it (contract open items #1–2 also live
    there).
- `_rows_from_csv(path)` / `_rows_from_xlsx(path, tab)` — thin loaders, both
  yield `dict[str, str|None]` keyed by header row.
- `sync_sheet(s: Settings, conn) -> int` — loaders → parsers → encrypted
  upserts, mirroring `sync_strava`'s shape; returns activity count.

### `store/db.py`

- `wellness` table: one column per `WellnessDay` field, PK
  `(athlete_id, local_date)`. `notes` joins the encrypted-columns set — it is
  the contract's PRIMARY prompt-injection surface and PII.
- `upsert_wellness(conn, days, *, key=None)` / `get_wellness(conn,
  athlete_id=None, *, key=None)` — parameterized binds only, same pattern and
  key handling as the activity functions.

### `normalize/join.py`

- `build_daily_rows(activities, wellness_days) -> list[DailyRow]`, pure.
  Contract rules, verbatim:
  - Group by `(athlete_id, local_date)`.
  - Sums: `session_count`, `tri_session_count` (via `Sport.is_tri`),
    `run_miles` (Run+VirtualRun), `bike_miles` (Ride+VirtualRide),
    `swim_miles`, `training_minutes`, `tri_training_minutes`,
    `elevation_gain_ft`, `total_suffer_score` (None if no activity has one).
  - Duration-weighted means (weights = `moving_time_sec` of activities where
    the metric is present): `avg_hr` across all, `avg_power_bike` /
    `weighted_power_bike` across bike activities, `avg_cadence_run` across
    runs. `avg_pace_run_min_per_mi` = total run minutes ÷ total run miles.
    `max_hr` = max.
  - `source_mix` = distinct sources, `activity_ids` = the day's ids.
  - Missing wellness → wellness field `None`; the day is NEVER dropped.
  - Wellness-only days (zero activities) still get a row — rest days are
    signal.

### `config.py` + `cli.py`

- New optional settings: `sheet_activities_path`, `sheet_wellness_path`
  (`SHEET_ACTIVITIES_PATH` / `SHEET_WELLNESS_PATH` in `.env`, documented in
  `.env.example`). Either CSV or xlsx; for xlsx the tab names default to
  `activities_raw` / `health_raw`.
- `synth sync` syncs every *configured* source: sheet when a path is set,
  Strava when client creds are set. A missing source is reported and skipped,
  not a crash (today it crashes without Strava creds).

## Error handling

Fail loudly at the boundary (contract: `extra="forbid"`): a malformed activity
row raises `ValueError` carrying the row's `activity_id` — no silent
row-skipping that quietly eats training data. Deliberate soft spots:
unknown sport → `OTHER` (existing normalizer behavior); absent or empty
wellness input → `[]` (documented normal state, not an error). A configured
activities path that doesn't exist is an error (the user asked for it).

## Security notes

- `name`, `device_name`, `notes` are `UntrustedText`: encrypted at rest via the
  existing seam, parameterized in SQL, and wrapped via
  `synthesize/prompts.wrap_untrusted()` before any future prompt use.
- The real export ("Triathlon Training Sync.xlsx - activities_raw.csv") is
  real personal data and stays gitignored; nothing in this work reads it in
  tests.

## Testing

Synthetic fixtures committed to `tests/fixtures/` (never the real export):

- `sheet_activities_sample.csv` (~10 rows): a 3-activity day, VirtualRun +
  VirtualRide, blank metric cells, `trainer=TRUE`, an unknown sport string,
  an 11:58 PM activity (pins the local-date join rule).
- `sheet_wellness_sample.csv`: a few rows including an injection-flavored
  `notes` value and a rest day (date with no activities).

Tests (TDD, no network):

1. Activity parser: field mapping, quirks, blank → None, unknown sport,
   malformed-row error carries the id.
2. Wellness parser: mapping, empty/missing input → `[]`.
3. xlsx loader: one test against a tiny workbook generated into `tmp_path`.
4. Wellness store: round-trip; `notes` is ciphertext on disk; wrong key fails.
5. Join: one test per contract rule (sums, each weighted mean's scoping,
   max, pace, missing wellness kept, wellness-only day kept, source_mix,
   multi-athlete separation).
6. `sync_sheet` wiring + `synth sync` source selection (sheet-only config no
   longer crashes on missing Strava creds).
