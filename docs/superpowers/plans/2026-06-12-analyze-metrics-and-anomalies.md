# Analyze Stage (Metrics + Anomalies) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `analyze/metrics.py` (`compute_metrics` + `detect_anomalies`), persist the results via new `daily_metrics`/`anomaly` store tables, and wire `synth analyze` in the CLI — per the locked v1.0 contract and the MVP design spec.

**Architecture:** Two pure functions (no I/O, no LLM) compute every metric relative to the athlete's own rolling calendar history; `DailyRow`s exist only for days with data, so the series is padded with zero-load calendar days before windowing. Detectors emit `Anomaly` rows with deterministic ids so `analyze` is idempotent. The CLI reads activities+wellness from SQLite, recomputes `DailyRow`s on demand (per the existing computed-not-stored decision), and upserts metrics+anomalies.

**Tech Stack:** Python 3.12, pydantic v2 contract models from `schemas.py`, stdlib `sqlite3` via `store/db.py`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-09-synth-mvp-design.md` (sections "Metrics", "Anomalies", "Data flow", "Testing").

## Locked design decisions (mirror into DECISIONS.md in Task 7)

- **Calendar padding:** rolling windows run over calendar days from the athlete's
  first to last `DailyRow`; a day with no row counts as 0 `training_minutes` and
  still emits a `DailyMetrics` with `rest_day=True` (continuous series for the
  agent; rest is signal).
- **Window gating:** `acute_load_7d` needs ≥7 calendar days of history;
  `chronic_load_28d` / `acwr` / `load_zscore_28d` need ≥28; z-score also `None`
  when the 28d std is 0; `acwr` `None` when chronic is 0. Std is population std.
- **Trends are split-half, run-days only:** 14d window = prior 7 calendar days
  vs recent 7; each half needs ≥2 days with the value present, else `None`.
  Trend % = (recent − prior) / prior × 100; positive = slowing / drifting up.
- **HR-at-pace = beats per mile** (`avg_hr × avg_pace_run_min_per_mi`): rising
  beats/mi at steady effort = aerobic decoupling.
- **Deterministic anomaly id:** `f"{athlete_id}:{local_date}:{metric}"` —
  re-running analyze upserts in place; the contract `Anomaly` has no
  `athlete_id` field, so the id carries it.
- **Load z-score fires high-side only** (z > 2 watch, z > 3 flag): the low side
  (detraining) is already ACWR < 0.8's job.
- **Wellness detectors (rhr/hrv):** baseline = the previous 28 calendar days'
  values (today excluded), gated on ≥14 values present and std > 0. RHR fires
  on z ≥ +2/+3 (elevated), HRV on z ≤ −2/−3 (suppressed). These are live now —
  the real workbook's wellness tabs populate rhr/hrv.

**File structure:**
- Create: `analyze/metrics.py` — both pure functions + threshold constants.
- Create: `tests/test_metrics.py`, `tests/test_anomalies.py`, `tests/test_metrics_store.py`.
- Modify: `store/db.py` — two tables + upsert/get pairs.
- Modify: `cli.py` — replace the `analyze` stub; `tests/test_sync_and_cli.py` updated.
- Modify: `DECISIONS.md` — thresholds + design entry.

---

### Task 1: `compute_metrics` — loads (acute, chronic, ACWR, z-score, rest_day)

**Files:**
- Create: `analyze/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metrics.py`:

```python
"""compute_metrics: rolling loads over a padded calendar series."""

from datetime import date, timedelta
from statistics import pstdev

from analyze.metrics import compute_metrics
from schemas import DailyRow, WellnessDay


def _day(d, minutes, *, athlete="ag", pace=None, hr=None, rhr=None, hrv=None):
    wellness = None
    if rhr is not None or hrv is not None:
        wellness = WellnessDay(
            local_date=date.fromisoformat(d), athlete_id=athlete, rhr=rhr, hrv=hrv
        )
    return DailyRow(
        local_date=date.fromisoformat(d), athlete_id=athlete, source_mix=[],
        session_count=1 if minutes else 0, tri_session_count=0,
        training_minutes=minutes, avg_pace_run_min_per_mi=pace, avg_hr=hr,
        wellness=wellness,
    )


def _streak(start="2026-05-01", days=28, minutes=60.0, **kw):
    d0 = date.fromisoformat(start)
    return [_day((d0 + timedelta(days=i)).isoformat(), minutes, **kw)
            for i in range(days)]


def test_acute_load_none_until_7_days_then_trailing_sum():
    ms = compute_metrics(_streak(days=8, minutes=60))
    assert [m.acute_load_7d for m in ms[:6]] == [None] * 6
    assert ms[6].acute_load_7d == 420            # 7 * 60
    assert ms[7].acute_load_7d == 420


def test_chronic_acwr_zscore_none_until_28_days():
    ms = compute_metrics(_streak(days=28, minutes=60))
    for m in ms[:27]:
        assert m.chronic_load_28d is None and m.acwr is None
        assert m.load_zscore_28d is None
    day28 = ms[27]
    assert day28.chronic_load_28d == 420         # mean 60 * 7
    assert day28.acwr == 1.0
    assert day28.load_zscore_28d is None         # constant load -> std 0


def test_zscore_matches_population_std():
    rows = _streak(days=27, minutes=60) + [_day("2026-05-28", 180.0)]
    m = compute_metrics(rows)[27]
    window = [60.0] * 27 + [180.0]
    mean = sum(window) / 28
    assert m.load_zscore_28d == (180.0 - mean) / pstdev(window)


def test_gap_days_count_as_zero_load_rest_days():
    rows = [_day("2026-06-01", 60.0), _day("2026-06-07", 60.0)]  # 5-day gap
    ms = compute_metrics(rows)
    assert len(ms) == 7                          # padded calendar series
    assert ms[3].rest_day is True and ms[3].acute_load_7d is None
    assert ms[6].acute_load_7d == 120            # 60 + five 0s + 60
    assert ms[0].rest_day is False and ms[6].rest_day is False


def test_acwr_none_when_chronic_is_zero():
    ms = compute_metrics(_streak(days=28, minutes=0.0))
    assert ms[27].chronic_load_28d == 0
    assert ms[27].acwr is None


def test_athletes_are_windowed_independently():
    rows = _streak(days=7, minutes=60, athlete="ag") + \
           _streak(days=7, minutes=30, athlete="basil")
    ms = compute_metrics(rows)
    by = {(m.athlete_id, m.local_date): m for m in ms}
    assert by[("ag", date(2026, 5, 7))].acute_load_7d == 420
    assert by[("basil", date(2026, 5, 7))].acute_load_7d == 210
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analyze.metrics'` (or ImportError).

- [ ] **Step 3: Write the implementation**

Create `analyze/metrics.py`:

```python
"""Deterministic training-load metrics + anomalies. Owner: Basil.

Pure functions — no I/O, no LLM. Every metric is relative to the athlete's OWN
rolling history (spec: docs/superpowers/specs/2026-06-09-synth-mvp-design.md).
DailyRows exist only for days with data, so windows run over a padded CALENDAR
series: a missing day is a real rest day (0 training minutes), not a hole.
Thresholds are tunable defaults, logged in DECISIONS.md.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from schemas import DailyMetrics, DailyRow

# --- tunable detector thresholds (DECISIONS.md) -----------------------------
ACWR_LOW = 0.8          # below = detraining (Gabbett sweet spot 0.8-1.3)
ACWR_HIGH = 1.3         # above = elevated injury risk
ACWR_FLAG = 1.5         # above = load-spike injury risk
ZSCORE_WATCH = 2.0
ZSCORE_FLAG = 3.0
TREND_WATCH_PCT = 5.0
TREND_FLAG_PCT = 10.0
MIN_TREND_DAYS_PER_HALF = 2     # run days needed in each 7d half-window
WELLNESS_WINDOW_DAYS = 28
MIN_WELLNESS_BASELINE_DAYS = 14


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5  # population std


def compute_metrics(daily_rows: list[DailyRow]) -> list[DailyMetrics]:
    by_athlete: dict[str, dict[date, DailyRow]] = defaultdict(dict)
    for r in daily_rows:
        by_athlete[r.athlete_id][r.local_date] = r

    out: list[DailyMetrics] = []
    for athlete_id in sorted(by_athlete):
        rows = by_athlete[athlete_id]
        first, last = min(rows), max(rows)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        minutes = [rows[d].training_minutes if d in rows else 0.0 for d in days]

        for i, d in enumerate(days):
            acute = sum(minutes[i - 6:i + 1]) if i >= 6 else None
            chronic = zscore = None
            if i >= 27:
                window = minutes[i - 27:i + 1]
                mean28 = _mean(window)
                chronic = mean28 * 7
                sd = _std(window)
                zscore = (minutes[i] - mean28) / sd if sd > 0 else None
            acwr = acute / chronic if acute is not None and chronic else None
            out.append(DailyMetrics(
                local_date=d,
                athlete_id=athlete_id,
                acute_load_7d=acute,
                chronic_load_28d=chronic,
                acwr=acwr,
                load_zscore_28d=zscore,
                rest_day=minutes[i] == 0,
            ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add analyze/metrics.py tests/test_metrics.py
git commit -m "feat(analyze): rolling loads, ACWR, 28d z-score over padded calendar series"
```

---

### Task 2: `compute_metrics` — 14d pace + HR-at-pace trends

**Files:**
- Modify: `analyze/metrics.py`
- Test: `tests/test_metrics.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
def test_pace_trend_recent_7d_vs_prior_7d_positive_means_slowing():
    rows = _streak(days=7, minutes=60, pace=10.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=11.0)
    ms = compute_metrics(rows)
    assert ms[12].pace_trend_pct_14d is None     # window incomplete
    assert ms[13].pace_trend_pct_14d == 10.0     # (11 - 10) / 10 * 100


def test_pace_trend_none_when_a_half_has_too_few_run_days():
    # prior half has a single paced day (< MIN_TREND_DAYS_PER_HALF)
    rows = [_day("2026-05-01", 60, pace=10.0)] + \
           _streak(start="2026-05-02", days=6, minutes=60) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=11.0)
    assert compute_metrics(rows)[13].pace_trend_pct_14d is None


def test_hr_at_pace_trend_uses_beats_per_mile():
    # beats/mi: prior 140*10=1400, recent 147*10=1470 -> +5%
    rows = _streak(days=7, minutes=60, pace=10.0, hr=140.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=10.0, hr=147.0)
    m = compute_metrics(rows)[13]
    assert round(m.hr_at_pace_trend_pct_14d, 6) == 5.0
    assert m.pace_trend_pct_14d == 0.0           # pace itself is steady


def test_hr_at_pace_needs_both_hr_and_pace():
    rows = _streak(days=7, minutes=60, pace=10.0) + \
           _streak(start="2026-05-08", days=7, minutes=60, pace=10.0, hr=150.0)
    assert compute_metrics(rows)[13].hr_at_pace_trend_pct_14d is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: the 4 new tests FAIL (trend fields are always `None`); the 6 existing tests still pass.

- [ ] **Step 3: Implement the trends**

In `analyze/metrics.py`, add the two helpers after `_std`:

```python
def _half_mean(values: list[float | None], min_n: int) -> float | None:
    present = [v for v in values if v is not None]
    return _mean(present) if len(present) >= min_n else None


def _trend_pct(series14: list[float | None]) -> float | None:
    """Recent 7 calendar days vs the prior 7. Positive = value rising."""
    prior = _half_mean(series14[:7], MIN_TREND_DAYS_PER_HALF)
    recent = _half_mean(series14[7:], MIN_TREND_DAYS_PER_HALF)
    if prior is None or recent is None or prior == 0:
        return None
    return (recent - prior) / prior * 100
```

Then replace the per-athlete loop body of `compute_metrics` so it builds the two
value series and fills the trend fields (full updated function):

```python
def compute_metrics(daily_rows: list[DailyRow]) -> list[DailyMetrics]:
    by_athlete: dict[str, dict[date, DailyRow]] = defaultdict(dict)
    for r in daily_rows:
        by_athlete[r.athlete_id][r.local_date] = r

    out: list[DailyMetrics] = []
    for athlete_id in sorted(by_athlete):
        rows = by_athlete[athlete_id]
        first, last = min(rows), max(rows)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        minutes = [rows[d].training_minutes if d in rows else 0.0 for d in days]
        pace = [rows[d].avg_pace_run_min_per_mi if d in rows else None for d in days]
        # HR-at-pace proxy: beats per mile (avg_hr [beats/min] * pace [min/mi]).
        beats_per_mi = [
            rows[d].avg_hr * rows[d].avg_pace_run_min_per_mi
            if d in rows and rows[d].avg_hr is not None
            and rows[d].avg_pace_run_min_per_mi is not None
            else None
            for d in days
        ]

        for i, d in enumerate(days):
            acute = sum(minutes[i - 6:i + 1]) if i >= 6 else None
            chronic = zscore = None
            if i >= 27:
                window = minutes[i - 27:i + 1]
                mean28 = _mean(window)
                chronic = mean28 * 7
                sd = _std(window)
                zscore = (minutes[i] - mean28) / sd if sd > 0 else None
            acwr = acute / chronic if acute is not None and chronic else None
            out.append(DailyMetrics(
                local_date=d,
                athlete_id=athlete_id,
                acute_load_7d=acute,
                chronic_load_28d=chronic,
                acwr=acwr,
                load_zscore_28d=zscore,
                pace_trend_pct_14d=(
                    _trend_pct(pace[i - 13:i + 1]) if i >= 13 else None
                ),
                hr_at_pace_trend_pct_14d=(
                    _trend_pct(beats_per_mi[i - 13:i + 1]) if i >= 13 else None
                ),
                rest_day=minutes[i] == 0,
            ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add analyze/metrics.py tests/test_metrics.py
git commit -m "feat(analyze): 14d pace + HR-at-pace (beats/mi) split-half trends"
```

---

### Task 3: `detect_anomalies` — ACWR + single-day load detectors

**Files:**
- Modify: `analyze/metrics.py`
- Test: `tests/test_anomalies.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anomalies.py`:

```python
"""detect_anomalies: each detector fires at its threshold, silent below it."""

from datetime import date

from analyze.metrics import detect_anomalies
from schemas import AnomalySeverity, DailyMetrics, DailyRow, WellnessDay


def _metric(d="2026-06-01", *, athlete="ag", **over):
    base = dict(local_date=date.fromisoformat(d), athlete_id=athlete)
    base.update(over)
    return DailyMetrics(**base)


def _row(d="2026-06-01", minutes=60.0, *, athlete="ag", rhr=None, hrv=None):
    wellness = None
    if rhr is not None or hrv is not None:
        wellness = WellnessDay(
            local_date=date.fromisoformat(d), athlete_id=athlete, rhr=rhr, hrv=hrv
        )
    return DailyRow(
        local_date=date.fromisoformat(d), athlete_id=athlete, source_mix=[],
        session_count=1, tri_session_count=0, training_minutes=minutes,
        wellness=wellness,
    )


def _only(anomalies, metric):
    return [a for a in anomalies if a.metric == metric]


def test_acwr_flag_above_1_5_watch_outside_safe_window_silent_inside():
    cases = {1.6: AnomalySeverity.FLAG, 1.4: AnomalySeverity.WATCH,
             0.7: AnomalySeverity.WATCH}
    for value, expected in cases.items():
        ms = [_metric(acwr=value, acute_load_7d=420.0, chronic_load_28d=300.0)]
        (a,) = _only(detect_anomalies([], ms), "acwr")
        assert a.severity == expected and a.value == value
    for value in (0.8, 1.0, 1.3):
        ms = [_metric(acwr=value, acute_load_7d=420.0, chronic_load_28d=420.0)]
        assert _only(detect_anomalies([], ms), "acwr") == []
    assert detect_anomalies([], [_metric()]) == []   # acwr None -> silent


def test_load_zscore_fires_high_side_only():
    rows = [_row(minutes=240.0)]
    flag = _metric(load_zscore_28d=3.5, chronic_load_28d=420.0)
    watch = _metric(load_zscore_28d=2.5, chronic_load_28d=420.0)
    (a,) = _only(detect_anomalies(rows, [flag]), "load_zscore_28d")
    assert a.severity == AnomalySeverity.FLAG
    assert a.value == 240.0                      # the day's training_minutes
    assert a.baseline == 60.0                    # chronic / 7 = 28d daily mean
    assert a.zscore == 3.5
    (a,) = _only(detect_anomalies(rows, [watch]), "load_zscore_28d")
    assert a.severity == AnomalySeverity.WATCH
    for z in (1.9, -2.5, -3.5, None):            # low side is ACWR's job
        assert _only(detect_anomalies(rows, [_metric(load_zscore_28d=z,
                     chronic_load_28d=420.0)]), "load_zscore_28d") == []


def test_anomaly_ids_are_deterministic():
    ms = [_metric(acwr=1.6, acute_load_7d=400.0, chronic_load_28d=250.0)]
    (a,) = detect_anomalies([], ms)
    (b,) = detect_anomalies([], ms)
    assert a.anomaly_id == b.anomaly_id == "ag:2026-06-01:acwr"


def test_descriptions_are_code_authored_and_carry_the_numbers():
    ms = [_metric(acwr=1.62, acute_load_7d=420.0, chronic_load_28d=259.0)]
    (a,) = detect_anomalies([], ms)
    assert "1.62" in a.description and "0.8" in a.description
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_anomalies.py -q`
Expected: FAIL — `ImportError: cannot import name 'detect_anomalies'`.

- [ ] **Step 3: Implement the two detectors**

Append to `analyze/metrics.py`:

```python
from schemas import Anomaly, AnomalySeverity  # add to the existing import line


def _anomaly(athlete_id: str, d: date, metric: str, value: float,
             severity: AnomalySeverity, description: str, *,
             baseline: float | None = None,
             zscore: float | None = None) -> Anomaly:
    # Deterministic id -> re-running analyze upserts in place. The contract
    # Anomaly has no athlete_id field, so the id carries it.
    return Anomaly(
        anomaly_id=f"{athlete_id}:{d.isoformat()}:{metric}",
        local_date=d, metric=metric, value=value, baseline=baseline,
        zscore=zscore, severity=severity, description=description,
    )


def _acwr_anomaly(m: DailyMetrics) -> Anomaly | None:
    if m.acwr is None:
        return None
    if m.acwr > ACWR_FLAG:
        sev = AnomalySeverity.FLAG
    elif m.acwr > ACWR_HIGH or m.acwr < ACWR_LOW:
        sev = AnomalySeverity.WATCH
    else:
        return None
    direction = "above" if m.acwr > ACWR_HIGH else "below"
    desc = (
        f"ACWR {m.acwr:.2f} is {direction} the safe window "
        f"{ACWR_LOW}-{ACWR_HIGH} (acute {m.acute_load_7d:.0f} min vs "
        f"chronic {m.chronic_load_28d:.0f} min)."
    )
    return _anomaly(m.athlete_id, m.local_date, "acwr", m.acwr, sev, desc)


def _load_zscore_anomaly(m: DailyMetrics, row: DailyRow | None) -> Anomaly | None:
    z = m.load_zscore_28d
    if z is None or z <= ZSCORE_WATCH:       # high side only; low = ACWR's job
        return None
    sev = AnomalySeverity.FLAG if z > ZSCORE_FLAG else AnomalySeverity.WATCH
    minutes = row.training_minutes if row is not None else 0.0
    baseline = m.chronic_load_28d / 7 if m.chronic_load_28d is not None else None
    desc = (
        f"Single-day load {minutes:.0f} min is z={z:+.1f} vs the 28d daily "
        f"mean of {baseline:.0f} min."
    )
    return _anomaly(m.athlete_id, m.local_date, "load_zscore_28d", minutes,
                    sev, desc, baseline=baseline, zscore=z)


def detect_anomalies(
    daily_rows: list[DailyRow], metrics: list[DailyMetrics]
) -> list[Anomaly]:
    rows = {(r.athlete_id, r.local_date): r for r in daily_rows}
    out: list[Anomaly] = []
    for m in metrics:
        row = rows.get((m.athlete_id, m.local_date))
        for candidate in (_acwr_anomaly(m), _load_zscore_anomaly(m, row)):
            if candidate is not None:
                out.append(candidate)
    return out
```

(Merge the `Anomaly, AnomalySeverity` names into the existing
`from schemas import ...` line rather than adding a duplicate import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_anomalies.py tests/test_metrics.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add analyze/metrics.py tests/test_anomalies.py
git commit -m "feat(analyze): ACWR + single-day load anomaly detectors with deterministic ids"
```

---

### Task 4: `detect_anomalies` — trend + wellness (rhr/hrv) detectors

**Files:**
- Modify: `analyze/metrics.py`
- Test: `tests/test_anomalies.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_anomalies.py`:

```python
from datetime import timedelta


def test_trend_detectors_watch_over_5pct_flag_over_10pct():
    for metric in ("pace_trend_pct_14d", "hr_at_pace_trend_pct_14d"):
        for value, expected in ((12.0, AnomalySeverity.FLAG),
                                (6.0, AnomalySeverity.WATCH)):
            (a,) = _only(detect_anomalies([], [_metric(**{metric: value})]), metric)
            assert a.severity == expected and a.value == value
        for value in (4.0, -6.0, -12.0, None):   # improving/steady = silent
            assert _only(detect_anomalies([], [_metric(**{metric: value})]),
                         metric) == []


def _wellness_history(values, day_of_interest_value, *, field="rhr"):
    """14 baseline days then the day under test (2026-06-15)."""
    d0 = date.fromisoformat("2026-06-01")
    rows = [_row((d0 + timedelta(days=i)).isoformat(), **{field: v})
            for i, v in enumerate(values)]
    rows.append(_row("2026-06-15", **{field: day_of_interest_value}))
    return rows


def test_rhr_elevated_vs_28d_baseline():
    baseline = [46.0, 48.0] * 7                  # mean 47, population std 1
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 50.0), []), "rhr")
    assert a.severity == AnomalySeverity.FLAG    # z = +3
    assert a.value == 50.0 and a.baseline == 47.0 and a.zscore == 3.0
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 49.5), []), "rhr")
    assert a.severity == AnomalySeverity.WATCH   # z = +2.5
    assert _only(detect_anomalies(_wellness_history(baseline, 48.0), []), "rhr") == []
    # LOW rhr is good news, never an anomaly
    assert _only(detect_anomalies(_wellness_history(baseline, 44.0), []), "rhr") == []


def test_hrv_suppressed_vs_28d_baseline():
    baseline = [60.0, 80.0] * 7                  # mean 70, population std 10
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 40.0, field="hrv"), []),
                 "hrv")
    assert a.severity == AnomalySeverity.FLAG    # z = -3
    (a,) = _only(detect_anomalies(_wellness_history(baseline, 45.0, field="hrv"), []),
                 "hrv")
    assert a.severity == AnomalySeverity.WATCH   # z = -2.5
    # HIGH hrv is good news, never an anomaly
    assert _only(detect_anomalies(_wellness_history(baseline, 100.0, field="hrv"), []),
                 "hrv") == []


def test_wellness_detectors_gate_on_baseline_days_and_variance():
    too_few = [46.0, 48.0] * 6                   # 12 < MIN_WELLNESS_BASELINE_DAYS
    assert _only(detect_anomalies(_wellness_history(too_few, 60.0), []), "rhr") == []
    flat = [47.0] * 14                           # std 0
    assert _only(detect_anomalies(_wellness_history(flat, 60.0), []), "rhr") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_anomalies.py -q`
Expected: the 4 new tests FAIL (no trend/wellness anomalies emitted); earlier tests still pass.

- [ ] **Step 3: Implement the detectors**

Append to `analyze/metrics.py`, and extend `detect_anomalies`:

```python
_TREND_DETECTORS = (
    ("pace_trend_pct_14d", "14d run pace slowed"),
    ("hr_at_pace_trend_pct_14d", "14d HR-at-pace (beats/mi) drifted up"),
)


def _trend_anomalies(m: DailyMetrics) -> list[Anomaly]:
    out = []
    for metric, label in _TREND_DETECTORS:
        value = getattr(m, metric)
        if value is None or value <= TREND_WATCH_PCT:
            continue
        sev = (AnomalySeverity.FLAG if value > TREND_FLAG_PCT
               else AnomalySeverity.WATCH)
        desc = f"{label} {value:+.1f}% vs the prior 7 days."
        out.append(_anomaly(m.athlete_id, m.local_date, metric, value, sev, desc))
    return out


# (field, direction, label): direction +1 fires on high z, -1 on low z.
_WELLNESS_DETECTORS = (
    ("rhr", 1, "elevated"),
    ("hrv", -1, "suppressed"),
)


def _wellness_anomalies(daily_rows: list[DailyRow]) -> list[Anomaly]:
    by_athlete: dict[str, list[DailyRow]] = defaultdict(list)
    for r in sorted(daily_rows, key=lambda r: (r.athlete_id, r.local_date)):
        if r.wellness is not None:
            by_athlete[r.athlete_id].append(r)

    out: list[Anomaly] = []
    for athlete_id, rows in by_athlete.items():
        for field, direction, label in _WELLNESS_DETECTORS:
            points = [(r.local_date, getattr(r.wellness, field))
                      for r in rows if getattr(r.wellness, field) is not None]
            for i, (d, value) in enumerate(points):
                window = [v for (dd, v) in points[:i]
                          if 0 < (d - dd).days <= WELLNESS_WINDOW_DAYS]
                if len(window) < MIN_WELLNESS_BASELINE_DAYS:
                    continue
                mean, sd = _mean(window), _std(window)
                if sd == 0:
                    continue
                z = (value - mean) / sd
                if direction * z >= ZSCORE_FLAG:
                    sev = AnomalySeverity.FLAG
                elif direction * z >= ZSCORE_WATCH:
                    sev = AnomalySeverity.WATCH
                else:
                    continue
                desc = (
                    f"{field.upper()} {value:.0f} is {label} vs its 28d "
                    f"baseline {mean:.0f} (z={z:+.1f})."
                )
                out.append(_anomaly(athlete_id, d, field, value, sev, desc,
                                    baseline=mean, zscore=z))
    return out
```

Update `detect_anomalies` to include both (full updated function):

```python
def detect_anomalies(
    daily_rows: list[DailyRow], metrics: list[DailyMetrics]
) -> list[Anomaly]:
    rows = {(r.athlete_id, r.local_date): r for r in daily_rows}
    out: list[Anomaly] = []
    for m in metrics:
        row = rows.get((m.athlete_id, m.local_date))
        for candidate in (_acwr_anomaly(m), _load_zscore_anomaly(m, row)):
            if candidate is not None:
                out.append(candidate)
        out.extend(_trend_anomalies(m))
    out.extend(_wellness_anomalies(daily_rows))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_anomalies.py tests/test_metrics.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add analyze/metrics.py tests/test_anomalies.py
git commit -m "feat(analyze): trend + wellness (rhr/hrv) anomaly detectors"
```

---

### Task 5: `daily_metrics` + `anomaly` tables in the store

**Files:**
- Modify: `store/db.py`
- Test: `tests/test_metrics_store.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metrics_store.py`:

```python
"""Round-trip + idempotency for the daily_metrics and anomaly tables."""

from datetime import date

from schemas import Anomaly, AnomalySeverity, DailyMetrics
from store import db


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def _metrics(d="2026-06-01", athlete="ag", **over):
    base = dict(
        local_date=date.fromisoformat(d), athlete_id=athlete,
        acute_load_7d=420.0, chronic_load_28d=300.0, acwr=1.4,
        load_zscore_28d=1.1, pace_trend_pct_14d=2.0,
        hr_at_pace_trend_pct_14d=-1.0, rest_day=False,
    )
    base.update(over)
    return DailyMetrics(**base)


def _anomaly(metric="acwr", d="2026-06-01", athlete="ag"):
    return Anomaly(
        anomaly_id=f"{athlete}:{d}:{metric}",
        local_date=date.fromisoformat(d), metric=metric, value=1.4,
        baseline=1.0, zscore=None, severity=AnomalySeverity.WATCH,
        description="ACWR 1.40 is above the safe window 0.8-1.3.",
    )


def test_metrics_roundtrip_and_idempotent_upsert(tmp_path):
    conn = _conn(tmp_path)
    m = _metrics()
    assert db.upsert_metrics(conn, [m, _metrics(d="2026-06-02", rest_day=True)]) == 2
    db.upsert_metrics(conn, [m])                 # same (athlete, date) again
    got = db.get_metrics(conn)
    assert len(got) == 2 and got[0] == m
    assert got[1].rest_day is True


def test_metrics_filtered_by_athlete(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_metrics(conn, [_metrics(), _metrics(athlete="basil", acwr=None)])
    got = db.get_metrics(conn, athlete_id="basil")
    assert [m.athlete_id for m in got] == ["basil"]
    assert got[0].acwr is None


def test_anomaly_roundtrip_and_idempotent_on_id(tmp_path):
    conn = _conn(tmp_path)
    a = _anomaly()
    assert db.upsert_anomalies(conn, [a]) == 1
    db.upsert_anomalies(conn, [a])               # same anomaly_id -> upsert
    got = db.get_anomalies(conn)
    assert got == [a]
    assert got[0].severity is AnomalySeverity.WATCH
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics_store.py -q`
Expected: FAIL — `AttributeError: module 'store.db' has no attribute 'upsert_metrics'`.

- [ ] **Step 3: Implement the tables**

In `store/db.py`, add column tuples + DDL after the split definitions
(everything is numeric/code-authored — `Anomaly.description` is generated by
our trusted code, never the LLM, so nothing here needs the encryption seam):

```python
# Derived analyze grain. All values are code-computed numerics/enums — the
# description is authored by OUR code (trusted, see schemas.Anomaly) — so no
# encrypted columns here; everything stays queryable.
METRICS_COLUMNS: tuple[str, ...] = (
    "athlete_id", "local_date", "acute_load_7d", "chronic_load_28d", "acwr",
    "load_zscore_28d", "pace_trend_pct_14d", "hr_at_pace_trend_pct_14d",
    "rest_day",
)
ANOMALY_COLUMNS: tuple[str, ...] = (
    "anomaly_id", "local_date", "metric", "value", "baseline", "zscore",
    "severity", "description",
)

_ANALYZE_DDL = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    athlete_id               TEXT NOT NULL,
    local_date               TEXT NOT NULL,
    acute_load_7d            REAL,
    chronic_load_28d         REAL,
    acwr                     REAL,
    load_zscore_28d          REAL,
    pace_trend_pct_14d       REAL,
    hr_at_pace_trend_pct_14d REAL,
    rest_day                 INTEGER NOT NULL,
    PRIMARY KEY (athlete_id, local_date)
);
CREATE TABLE IF NOT EXISTS anomaly (
    anomaly_id   TEXT PRIMARY KEY,
    local_date   TEXT NOT NULL,
    metric       TEXT NOT NULL,
    value        REAL NOT NULL,
    baseline     REAL,
    zscore       REAL,
    severity     TEXT NOT NULL,
    description  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_anomaly_date ON anomaly (local_date);
"""
```

Extend `init_db`:

```python
def init_db(conn: sqlite3.Connection) -> None:
    # executescript() issues an implicit COMMIT; do not call mid-transaction.
    conn.executescript(_ACTIVITY_DDL + _WELLNESS_DDL + _SPLITS_DDL + _ANALYZE_DDL)
```

Add the import (`DailyMetrics`, `Anomaly` join the existing `from schemas import ...`
line) and the four functions at the end of the file:

```python
def upsert_metrics(conn: sqlite3.Connection, metrics: list[DailyMetrics]) -> int:
    cols = ", ".join(METRICS_COLUMNS)
    placeholders = ", ".join("?" for _ in METRICS_COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in METRICS_COLUMNS
        if c not in ("athlete_id", "local_date")
    )
    sql = (
        f"INSERT INTO daily_metrics ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(athlete_id, local_date) DO UPDATE SET {updates}"
    )
    rows = [tuple(m.model_dump(mode="json")[c] for c in METRICS_COLUMNS)
            for m in metrics]
    with conn:  # security: all values parameterized; no f-string values
        conn.executemany(sql, rows)
    return len(rows)


def get_metrics(
    conn: sqlite3.Connection, athlete_id: str | None = None
) -> list[DailyMetrics]:
    if athlete_id is None:
        cur = conn.execute("SELECT * FROM daily_metrics ORDER BY athlete_id, local_date")
    else:
        cur = conn.execute(
            "SELECT * FROM daily_metrics WHERE athlete_id = ? ORDER BY local_date",
            (athlete_id,),
        )
    return [DailyMetrics.model_validate(dict(r)) for r in cur.fetchall()]


def upsert_anomalies(conn: sqlite3.Connection, anomalies: list[Anomaly]) -> int:
    cols = ", ".join(ANOMALY_COLUMNS)
    placeholders = ", ".join("?" for _ in ANOMALY_COLUMNS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in ANOMALY_COLUMNS if c != "anomaly_id"
    )
    sql = (
        f"INSERT INTO anomaly ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(anomaly_id) DO UPDATE SET {updates}"
    )
    rows = [tuple(a.model_dump(mode="json")[c] for c in ANOMALY_COLUMNS)
            for a in anomalies]
    with conn:  # security: all values parameterized; no f-string values
        conn.executemany(sql, rows)
    return len(rows)


def get_anomalies(conn: sqlite3.Connection) -> list[Anomaly]:
    cur = conn.execute("SELECT * FROM anomaly ORDER BY local_date, anomaly_id")
    return [Anomaly.model_validate(dict(r)) for r in cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics_store.py tests/test_db.py -q`
Expected: all pass (existing store tests must stay green).

- [ ] **Step 5: Commit**

```bash
git add store/db.py tests/test_metrics_store.py
git commit -m "feat(store): daily_metrics + anomaly tables with idempotent upserts"
```

---

### Task 6: `synth analyze` CLI wiring

**Files:**
- Modify: `cli.py`
- Test: `tests/test_sync_and_cli.py` (modify one test, add two)

- [ ] **Step 1: Update existing test + write new failing tests**

In `tests/test_sync_and_cli.py`, replace
`test_cli_analyze_and_report_are_stubs_for_now` (analyze is no longer a stub):

```python
def test_cli_report_is_a_stub_for_now(capsys):
    assert cli.main(["report"]) == 0
    assert "follow-on plan" in capsys.readouterr().out
```

Append (uses the existing `_settings` helper; data flows through the real
store + join + analyze stack — only settings are faked):

```python
def _stored_run(conn, key, i, day, minutes=60.0):
    from datetime import datetime
    from schemas import Activity, Source, Sport
    start = datetime.fromisoformat(f"{day}T07:00:00")
    db.upsert_activities(conn, [Activity(
        activity_id=f"r{i}", source=Source.SHEET, athlete_id="ag",
        start_local=start, local_date=start.date(), name=f"run {i}",
        sport=Sport.RUN, moving_time_sec=minutes * 60, distance_mi=5.0,
    )], key=key)


def test_cli_analyze_computes_and_persists_metrics_and_anomalies(
    tmp_path, monkeypatch, capsys
):
    from datetime import date, timedelta
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    d0 = date(2026, 5, 1)
    for i in range(28):                          # steady base ...
        _stored_run(conn, key, i, (d0 + timedelta(days=i)).isoformat())
    _stored_run(conn, key, 99, "2026-05-29", minutes=300.0)  # ... then a spike

    assert cli.main(["analyze"]) == 0
    out = capsys.readouterr().out
    assert "29 days" in out and "daily metrics" in out
    assert "HUSH_CLIENT_SECRET" not in out       # safe_summary() only

    metrics = db.get_metrics(conn)
    assert len(metrics) == 29                    # one per calendar day
    assert any(a.metric == "load_zscore_28d" for a in db.get_anomalies(conn))


def test_cli_analyze_with_empty_db_fails_clearly(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    assert cli.main(["analyze"]) == 1
    assert "nothing to analyze" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest tests/test_sync_and_cli.py -q`
Expected: the two new analyze tests FAIL (stub prints "follow-on plan", returns 0); the rest pass.

- [ ] **Step 3: Wire the command**

In `cli.py`: add imports, add `_cmd_analyze`, and keep the stub only for `report`.

```python
from collections import Counter

from analyze.metrics import compute_metrics, detect_anomalies
from normalize.join import build_daily_rows
from security import crypto
```

```python
def _cmd_analyze(_args: argparse.Namespace) -> int:
    s = get_settings()
    print("config:", s.safe_summary())  # redacted — never prints secrets
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    key = crypto.load_or_create_key(s.encryption_key_path)
    activities = db.get_activities(conn, key=key)
    wellness = db.get_wellness(conn, key=key)
    if not activities and not wellness:
        print("nothing to analyze: run `synth sync` first")
        return 1
    daily_rows = build_daily_rows(activities, wellness)
    metrics = compute_metrics(daily_rows)
    anomalies = detect_anomalies(daily_rows, metrics)
    db.upsert_metrics(conn, metrics)
    db.upsert_anomalies(conn, anomalies)
    by_severity = Counter(a.severity.value for a in anomalies)
    print(
        f"analyze: {len(daily_rows)} days -> {len(metrics)} daily metrics, "
        f"{len(anomalies)} anomalies {dict(sorted(by_severity.items()))}"
    )
    return 0
```

In `build_parser()`, replace the stub loop:

```python
    analyze = sub.add_parser("analyze", help="compute training-load metrics + anomalies")
    analyze.set_defaults(func=_cmd_analyze)

    sub.add_parser("report").set_defaults(func=_cmd_stub("report"))
```

Also update the module docstring (line 1) to
`"""synth CLI. sync + analyze are wired; report lands with the agent plan."""`
and the `_cmd_stub` message stays as-is (still true for `report`).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_sync_and_cli.py
git commit -m "feat(cli): wire synth analyze -> metrics + anomalies persisted"
```

---

### Task 7: DECISIONS.md entry + real-workbook smoke test

**Files:**
- Modify: `DECISIONS.md`

- [ ] **Step 1: Append the decision entry**

Append to `DECISIONS.md`:

```markdown
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
```

- [ ] **Step 2: Full suite + real-workbook smoke test**

Run: `uv run pytest -q` — expected: all pass.

Then smoke-test against the real workbook (already configured in `.env` /
gitignored at the repo root):

```bash
uv run synth sync
uv run synth analyze
```

Expected: `analyze: N days -> N daily metrics, M anomalies {...}` with N ≈ the
workbook's date span and at least some anomalies (the spike days and rhr/hrv
deviations in the real data). Eyeball a couple:

```bash
sqlite3 synth.db "SELECT local_date, metric, value, severity FROM anomaly ORDER BY local_date DESC LIMIT 10"
```

Sanity-check that descriptions read correctly and no ciphertext or secrets
appear in the output.

- [ ] **Step 3: Commit**

```bash
git add DECISIONS.md
git commit -m "docs: record analyze-stage windowing, trend, and threshold decisions"
```
