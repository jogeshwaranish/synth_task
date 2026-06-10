# Interface Contract — synth MVP (v1.0)

One file (`schemas.py`) defines every shape that crosses the boundary between
the two workstreams. If it's not in there, it's an implementation detail and
the other person shouldn't depend on it.

## Ownership

| Model | Produced by | Consumed by |
|---|---|---|
| `Activity`, `RunSplit`, `BikeSplit`, `SwimSplit` | Anish (ingest + validation) | Basil (agent tools) |
| `WellnessDay` | Anish | Basil |
| `DailyRow` (the join) | Anish | Basil (metrics + agent) |
| `DailyMetrics`, `Anomaly` | Basil (analyze/) | Basil's agent; Anish's tests |
| `SynthesisReport` + `insight_schema.json` | Basil's agent emits it | **Anish's validator enforces it** |

## The join (canonical decisions — copy into DECISIONS.md)

- **Grain:** two grains in the store. Per-activity (plus splits) for agent
  drill-down; per-day `DailyRow` for the wellness join and all heuristics.
- **Join key:** `local_date` derived from `start_date_local`. Never UTC — an
  11:58 PM workout belongs to the day the athlete experienced it.
- **Multi-activity days** (AG's sheet has up to 6/day): sum volume fields,
  duration-weight the averages, max the maxes.
- **Missing wellness:** wellness fields stay `None`; the day is never dropped.
  AG's wellness tabs are empty as of June 9 — treat late-arriving rows as the
  normal case, not an edge case.
- **Zero-activity days with wellness data still get a `DailyRow`** — rest
  days are signal.

## Security hooks baked into the contract

- `UntrustedText` marks every field whose content originates outside our code
  (sheet cells, Strava activity names, wellness notes). These are wrapped in
  data delimiters before any prompt and parameterized before any query.
- `extra="forbid"` everywhere: unexpected fields from the sheet or from the
  LLM fail loudly at the boundary instead of flowing through.
- `Evidence.tool` is a closed `Literal` — a hijacked model response cannot
  claim to have called a tool that doesn't exist, and the harness (not the
  LLM) writes the trace anyway.
- The LLM's JSON is validated against `insight_schema.json` before anything
  downstream touches it. Validation failure = report rejected + logged.

## Change protocol

Breaking change → bump `CONTRACT_VERSION`, regenerate `insight_schema.json`
(`python schemas.py`), ping the other person, one-line entry in DECISIONS.md.
Additive optional fields don't need a version bump.

## Known open items (v1.0)

1. `WellnessDay.snoring` units unknown (tab is empty) — passing through raw.
2. `in_bed` / `asleep` assumed **hours**; confirm when AG populates or answers.
3. `athlete_id` is a plain string ("ag" / "basil" / "anish") — fine for MVP,
   becomes a real entity if multi-athlete ever matters.
