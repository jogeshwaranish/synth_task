"""
synth MVP — interface contract (v1)

Single source of truth for the shapes that cross the Basil <-> Anish boundary.
Anish's pipeline PRODUCES: Activity, WellnessDay, DailyRow (+ split models).
Basil's layer PRODUCES: DailyMetrics, Anomaly, SynthesisReport.

Rules:
- Bump CONTRACT_VERSION on any breaking change, and tell the other person.
- All numeric fields are strictly typed. Coercion failures at ingestion are
  rejected + logged, never silently passed through (Anish's validation layer).
- Fields typed `UntrustedText` carry sheet- or Strava-authored free text.
  They must NEVER be interpolated into prompts without delimiter wrapping
  (see synthesize/prompts.py) and never into SQL without parameterization.

Run `python schemas.py` to emit insight_schema.json (JSON Schema for the
synthesis output, used by Anish's response validator).
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"

# Free text authored outside our codebase (sheet cells, Strava activity names).
# Treat as data, never as instructions.
UntrustedText = Annotated[str, Field(max_length=2000)]


class Source(str, Enum):
    STRAVA_API = "strava_api"   # pulled live from Strava (Basil's / Anish's accounts)
    SHEET = "sheet"             # AG's Google Sheet


class Sport(str, Enum):
    RUN = "Run"
    VIRTUAL_RUN = "VirtualRun"
    RIDE = "Ride"
    VIRTUAL_RIDE = "VirtualRide"
    SWIM = "Swim"
    WALK = "Walk"
    STRENGTH = "WeightTraining"
    OTHER = "Other"

    @classmethod
    def normalize(cls, raw: str) -> "Sport":
        try:
            return cls(raw)
        except ValueError:
            return cls.OTHER

    @property
    def is_tri(self) -> bool:
        return self in {Sport.RUN, Sport.VIRTUAL_RUN, Sport.RIDE,
                        Sport.VIRTUAL_RIDE, Sport.SWIM}


class StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# ---------------------------------------------------------------------------
# Ingestion grain 1: per-activity  (producer: Anish)
# ---------------------------------------------------------------------------

class Activity(StrictBase):
    activity_id: str                      # Strava id, or sheet id like "A0001"
    source: Source
    athlete_id: str                       # "ag" | "basil" | "anish"
    start_local: datetime
    start_utc: datetime | None = None
    local_date: Date                      # JOIN KEY. Derived from start_local.
    name: UntrustedText
    sport: Sport
    is_trainer: bool = False
    moving_time_sec: float = Field(ge=0)
    elapsed_time_sec: float | None = Field(default=None, ge=0)
    distance_mi: float = Field(ge=0)
    elevation_gain_ft: float | None = Field(default=None, ge=0)
    avg_speed_mph: float | None = Field(default=None, ge=0)
    avg_hr: float | None = Field(default=None, ge=20, le=250)
    max_hr: float | None = Field(default=None, ge=20, le=250)
    avg_watts: float | None = Field(default=None, ge=0)
    weighted_watts: float | None = Field(default=None, ge=0)
    kilojoules: float | None = Field(default=None, ge=0)
    avg_cadence: float | None = None      # Strava convention: run = single-leg
    suffer_score: float | None = Field(default=None, ge=0)
    calories: float | None = Field(default=None, ge=0)
    perceived_exertion: float | None = Field(default=None, ge=0, le=10)
    device_name: UntrustedText | None = None


# Split grain (producer: Anish, parsed from sheet *_splits_raw tabs).
# Consumed only by Basil's agent tools — not part of the daily join.

class RunSplit(StrictBase):
    activity_id: str
    split_index: int = Field(ge=1)
    distance_mi: float = Field(ge=0)
    moving_time_sec: float = Field(ge=0)
    pace_min_per_mi: float | None = Field(default=None, gt=0)
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cadence_run: float | None = None  # already doubled (steps/min)
    elevation_gain_ft: float | None = None
    is_partial: bool = False


class BikeSplit(StrictBase):
    activity_id: str
    split_index: int = Field(ge=1)
    duration_sec: float = Field(ge=0)
    distance_mi: float = Field(ge=0)
    avg_speed_mph: float | None = None
    avg_hr: float | None = None
    avg_power: float | None = Field(default=None, ge=0)
    avg_cadence: float | None = None
    elevation_gain_ft: float | None = None
    is_partial: bool = False


class SwimSplit(StrictBase):
    activity_id: str
    split_index: int = Field(ge=1)
    swim_context: Literal["pool", "open_water"] | None = None
    distance: float = Field(ge=0)
    distance_unit: Literal["yd", "m"] = "yd"
    duration_sec: float = Field(ge=0)
    pace_sec_per_100: float | None = Field(default=None, gt=0)
    stroke_style: UntrustedText | None = None
    swolf: float | None = Field(default=None, ge=0)
    avg_hr: float | None = None


# ---------------------------------------------------------------------------
# Ingestion grain 2: per-day wellness  (producer: Anish, from sheet
# manual_notes / health_raw — currently EMPTY tabs; rows may arrive late)
# ---------------------------------------------------------------------------

class WellnessDay(StrictBase):
    local_date: Date                      # JOIN KEY
    athlete_id: str
    in_bed_hours: float | None = Field(default=None, ge=0, le=24)
    asleep_hours: float | None = Field(default=None, ge=0, le=24)
    snoring: float | None = None          # units unknown; pass through raw
    rhr: float | None = Field(default=None, ge=25, le=120)
    hrv: float | None = Field(default=None, ge=0, le=300)
    body_weight_lb: float | None = Field(default=None, gt=0, le=500)
    sauna_mins: float | None = Field(default=None, ge=0)
    notes: UntrustedText | None = None    # PRIMARY PROMPT-INJECTION SURFACE


# ---------------------------------------------------------------------------
# The joined daily table  (producer: Anish; one row per athlete per date)
# Join rules (DECISIONS.md #3):
#   - local_date from start_local, NOT UTC. 11:58 PM workout belongs to that day.
#   - multiple activities/day: sums for volume, duration-weighted mean for
#     avg_hr/power/cadence/pace, max for max_hr.
#   - missing wellness row -> wellness fields None. NEVER drop the day.
#   - days with zero activities still get a row if wellness exists (rest days
#     are signal, not absence of data).
# ---------------------------------------------------------------------------

class DailyRow(StrictBase):
    local_date: Date
    athlete_id: str
    source_mix: list[Source]
    # volume
    session_count: int = Field(ge=0)
    tri_session_count: int = Field(ge=0)
    run_miles: float = 0
    bike_miles: float = 0
    swim_miles: float = 0
    training_minutes: float = 0
    tri_training_minutes: float = 0
    elevation_gain_ft: float = 0
    # intensity (duration-weighted across the day's activities)
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power_bike: float | None = None
    weighted_power_bike: float | None = None
    avg_cadence_run: float | None = None
    avg_pace_run_min_per_mi: float | None = None
    total_suffer_score: float | None = None
    # wellness (None until AG's tabs populate)
    wellness: WellnessDay | None = None
    activity_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Derived metrics  (producer: Basil, analyze/metrics.py — deterministic code,
# no LLM. These are also what query_anomalies() serves to the agent.)
# ---------------------------------------------------------------------------

class DailyMetrics(StrictBase):
    local_date: Date
    athlete_id: str
    acute_load_7d: float | None = None        # 7d rolling sum, training_minutes
    chronic_load_28d: float | None = None     # 28d rolling daily mean * 7
    acwr: float | None = None                 # acute / chronic; None if <28d history
    load_zscore_28d: float | None = None
    pace_trend_pct_14d: float | None = None   # +ve = slowing
    hr_at_pace_trend_pct_14d: float | None = None
    rest_day: bool = False


class AnomalySeverity(str, Enum):
    INFO = "info"
    WATCH = "watch"
    FLAG = "flag"


class Anomaly(StrictBase):
    anomaly_id: str
    local_date: Date
    metric: str                               # e.g. "acwr", "avg_pace_run"
    value: float
    baseline: float | None = None
    zscore: float | None = None
    severity: AnomalySeverity
    description: str                          # generated by OUR code — trusted


# ---------------------------------------------------------------------------
# Synthesis output  (producer: Basil's agent; validator: Anish)
# The LLM must return JSON conforming to SynthesisReport minus the fields
# marked "filled by harness". Anish's validator rejects anything that doesn't
# parse — a hijacked/malformed response never propagates.
# ---------------------------------------------------------------------------

class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Evidence(StrictBase):
    """One step of the agent's tool-call trace. Filled by harness, not the LLM."""
    step: int
    tool: Literal["get_daily_metrics", "get_activity_detail",
                  "compare_periods", "query_anomalies"]
    args: dict
    result_digest: str = Field(max_length=500)


class Pattern(StrictBase):
    pattern_id: str
    title: str = Field(max_length=120)
    description: str = Field(max_length=1200)
    kind: Literal["trend", "correlation", "anomaly_explanation", "observation"]
    date_start: Date
    date_end: Date
    metrics_involved: list[str]
    supporting_activity_ids: list[str] = Field(default_factory=list)
    confidence: Confidence
    caveats: str | None = Field(default=None, max_length=400)


class SynthesisReport(StrictBase):
    contract_version: Literal["1.0"] = CONTRACT_VERSION   # filled by harness
    report_id: str                                        # filled by harness
    generated_at: datetime                                # filled by harness
    athlete_id: str
    period_start: Date
    period_end: Date
    data_coverage: dict = Field(
        default_factory=dict,
        description="n_days, n_activities, n_wellness_days — filled by harness",
    )
    summary: str = Field(max_length=2000)
    patterns: list[Pattern] = Field(max_length=10)
    anomalies_reviewed: list[str] = Field(
        default_factory=list,
        description="anomaly_ids the agent examined, addressed or dismissed",
    )
    open_questions: list[str] = Field(
        default_factory=list, max_length=5,
        description="What more data would resolve, e.g. empty wellness tabs",
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Tool-call trace — filled by harness, shown to AG",
    )


if __name__ == "__main__":
    schema = SynthesisReport.model_json_schema()
    with open("insight_schema.json", "w") as f:
        json.dump(schema, f, indent=2)
    print(f"contract v{CONTRACT_VERSION} -> insight_schema.json")
