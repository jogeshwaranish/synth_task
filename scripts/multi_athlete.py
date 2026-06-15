"""Run the synth app across EVERY athlete in AG's pivoted rowing workbook.

Owner: Anish (data-pipeline harness). NOT part of the shipped app — it drives the
existing, unchanged pipeline (ingest/rowing, normalize/join, analyze/*, store/db)
for the whole roster instead of one athlete, so a coach can see how the SAME
system surfaces a DIFFERENT pattern for each person.

AG's ask: "extract individual athlete trends … find different patterns for each
athlete." We have real erg results per athlete but no Strava for them, so for each
athlete we generate a LEAN slice of fake Strava (enough days to surface a pattern,
far less than the sheet's full season) whose shape is PLANTED FROM that athlete's
own erg trajectory:

  1. `classify_trend` reads the athlete's real erg splits -> adapting / plateau /
     overreaching (+ the inflection date where a non-improver stalled).
  2. `plan_pattern` is the repo's "LLM as config compiler" pattern (cf.
     ingest/mapping.py): an LLM sees ONLY the computed trend numbers (no names, no
     untrusted free text) and emits a small `PatternConfig` (window, when/how hard
     the overload lands, how far recovery markers move). `validate_pattern`
     bounds-checks it before any use; bad output is rejected + logged and we fall
     back to a deterministic `default_pattern`.
  3. `simulate` deterministically EXPANDS that validated config into daily
     activities + wellness (RNG only adds per-athlete jitter; the config owns the
     pattern shape). The LLM narrative is never written into the DB.

The combined DB is written with `key=None` (PLAINTEXT) on purpose: it is a
committed, portable test artifact and the at-rest key is per-machine, so an
encrypted DB couldn't be opened on another checkout. Production `synth sync` still
passes a real `key=` and encrypts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from analyze.metrics import compute_metrics, detect_anomalies
from analyze.rowing import (
    ERG_PLATEAU_DAYS, ERG_REG_FLAG_PCT, _piece, _split_sec,
    detect_erg_anomalies,
)
from normalize.join import build_daily_rows
from schemas import Activity, Source, Sport, WellnessDay
from security import crypto
from store import db
from synthesize.prompts import wrap_untrusted

logger = logging.getLogger(__name__)

# A "lean" Strava window: long enough for a 28d chronic baseline + an overload
# block to register in ACWR / wellness z-scores, but a fraction of the erg season.
MIN_WINDOW_DAYS = 49
MAX_WINDOW_DAYS = 98
DEFAULT_WINDOW_DAYS = 77            # ~11 weeks
MIN_PIECE_TESTS = 4                 # need a few comparable tests to call a trend
_SEASON_LO = date(2025, 8, 1)
_SEASON_HI = date(2026, 9, 1)


class PatternRejected(Exception):
    """LLM pattern config failed validation — caller falls back to default."""


# --------------------------------------------------------------------------
# Step 1: read each athlete's real erg trajectory
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AthleteTrend:
    athlete_id: str
    piece: str
    n_tests: int
    first_date: date
    best_split: float          # sec/500m (lower = faster)
    best_date: date
    last_split: float
    last_date: date
    improvement_sec: float     # first_split - last_split (positive = got faster)
    last_off_best_pct: float   # how far the latest test sits off the season best
    category: str              # "adapting" | "plateau" | "overreaching"

    def fingerprint(self) -> str:
        payload = json.dumps({
            "piece": self.piece, "n": self.n_tests,
            "best": round(self.best_split, 1), "last": round(self.last_split, 1),
            "imp": round(self.improvement_sec, 1),
            "off": round(self.last_off_best_pct, 1), "cat": self.category,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _erg_series(erg_acts: list[Activity]) -> dict[str, list[tuple[date, float]]]:
    by_piece: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for a in erg_acts:
        if a.source is not Source.SHEET or a.sport is not Sport.OTHER:
            continue
        s = _split_sec(a)
        if s is not None:
            by_piece[_piece(a)].append((a.local_date, s))
    return by_piece


def classify_trend(erg_acts: list[Activity]) -> AthleteTrend | None:
    """Pick the most-tested erg piece family for this athlete and classify its
    season trajectory. Returns None if no family has enough comparable tests."""
    by_piece = _erg_series(erg_acts)
    if not by_piece:
        return None
    piece, series = max(by_piece.items(), key=lambda kv: len(kv[1]))
    if len(series) < MIN_PIECE_TESTS:
        return None
    series.sort()
    athlete_id = erg_acts[0].athlete_id
    first_date, first_split = series[0]
    last_date, last_split = series[-1]
    best_date, best_split = min(series, key=lambda ds: ds[1])
    improvement = first_split - last_split
    off_best = (last_split - best_split) / best_split * 100

    if off_best >= ERG_REG_FLAG_PCT:
        category = "overreaching"
    elif improvement >= 2.0 and off_best <= 1.0:
        category = "adapting"
    else:
        category = "plateau"

    return AthleteTrend(
        athlete_id=athlete_id, piece=piece, n_tests=len(series),
        first_date=first_date, best_split=best_split, best_date=best_date,
        last_split=last_split, last_date=last_date,
        improvement_sec=improvement, last_off_best_pct=off_best,
        category=category,
    )


# --------------------------------------------------------------------------
# Step 2: the LLM plants the pattern (config compiler), bounded by validation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PatternConfig:
    window_start: date
    window_end: date
    baseline_load: float          # ~1.0; scales session volume
    overload_start: date | None   # None for a clean (adapting) athlete
    overload_peak_factor: float   # load multiplier reached by window_end
    hrv_drop: float               # ms HRV falls across the overload
    rhr_rise: float               # bpm RHR climbs across the overload
    sleep_dip: float              # hours of sleep lost during the overload


def default_pattern(trend: AthleteTrend) -> PatternConfig:
    """Deterministic config derived straight from the trend. Used when no LLM is
    supplied and as the fallback when the LLM's output fails validation."""
    end = trend.last_date
    start = end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if trend.category == "adapting":
        return PatternConfig(start, end, 1.0, None, 1.05, 0.0, 0.0, 0.0)
    # plateau / overreaching stall at best_date; overload begins around then,
    # clamped to leave a >=21d chronic baseline and a >=7d tail inside the window.
    lo, hi = start + timedelta(days=21), end - timedelta(days=7)
    overload_start = min(max(trend.best_date, lo), hi)
    if trend.category == "overreaching":
        return PatternConfig(start, end, 1.0, overload_start, 1.6, 16.0, 9.0, 1.4)
    return PatternConfig(start, end, 1.0, overload_start, 1.22, 7.0, 4.0, 0.6)


def _build_prompt(trend: AthleteTrend) -> str:
    # Only computed numbers cross to the model — no athlete name / untrusted text.
    facts = wrap_untrusted(
        f"erg_piece={trend.piece}\n"
        f"tests={trend.n_tests}\n"
        f"category={trend.category}\n"
        f"improvement_sec={trend.improvement_sec:.1f}  "
        f"(positive = got faster over the season)\n"
        f"latest_is_off_season_best_by_pct={trend.last_off_best_pct:.1f}\n"
        f"first_test_date={trend.first_date.isoformat()}\n"
        f"season_best_date={trend.best_date.isoformat()}\n"
        f"latest_test_date={trend.last_date.isoformat()}\n"
    )
    return (
        "You design a SHORT, realistic simulated training block for ONE rower so a "
        "coaching tool can be demonstrated. You are given that rower's erg-test "
        "trajectory (numbers only). Emit a JSON config that PLANTS a training/"
        "recovery pattern consistent with it:\n"
        "  - adapting  -> healthy block: load matched, recovery markers stable, "
        "no overload.\n"
        "  - plateau   -> a mild overload that stalls progress.\n"
        "  - overreaching -> a clear overload: load ramps, HRV drops, RHR rises, "
        "sleep dips.\n\n"
        "Keep the window LEAN: 7-14 weeks ending on latest_test_date, with the "
        "overload (if any) beginning near season_best_date. Fields:\n"
        '  window_start, window_end: "YYYY-MM-DD"\n'
        "  baseline_load: 0.8-1.2\n"
        '  overload_start: "YYYY-MM-DD" or null (null ONLY for adapting)\n'
        "  overload_peak_factor: 1.0-1.8 (~1.0 for adapting)\n"
        "  hrv_drop: 0-25 ms   rhr_rise: 0-15 bpm   sleep_dip: 0-2.5 h "
        "(all ~0 for adapting)\n\n"
        "Here is the trajectory:\n\n" + facts +
        "\n\nRespond with ONLY the JSON object, no prose, no code fence."
    )


def _parse_date(v: object) -> date:
    return date.fromisoformat(str(v))


def validate_pattern(data: object, trend: AthleteTrend) -> PatternConfig:
    """Bounds-check the LLM output before it can shape any data. Mirrors the
    reject-on-bad-output stance of ingest/rowing.validate_mapping."""
    if not isinstance(data, dict):
        raise PatternRejected("pattern output was not a JSON object")
    try:
        ws, we = _parse_date(data["window_start"]), _parse_date(data["window_end"])
        baseline = float(data["baseline_load"])
        peak = float(data["overload_peak_factor"])
        hrv_drop = float(data["hrv_drop"])
        rhr_rise = float(data["rhr_rise"])
        sleep_dip = float(data["sleep_dip"])
        raw_ovl = data.get("overload_start")
        overload_start = _parse_date(raw_ovl) if raw_ovl not in (None, "") else None
    except (KeyError, ValueError, TypeError) as e:
        raise PatternRejected(f"pattern missing/!malformed field: {e}") from e

    span = (we - ws).days
    if not (MIN_WINDOW_DAYS <= span <= MAX_WINDOW_DAYS):
        raise PatternRejected(f"window {span}d not lean ({MIN_WINDOW_DAYS}-{MAX_WINDOW_DAYS})")
    if not (_SEASON_LO <= ws < we <= _SEASON_HI):
        raise PatternRejected("window outside the season range")
    if not (0.8 <= baseline <= 1.2):
        raise PatternRejected(f"baseline_load {baseline} out of range")
    if not (1.0 <= peak <= 1.8):
        raise PatternRejected(f"overload_peak_factor {peak} out of range")
    if not (0.0 <= hrv_drop <= 25 and 0.0 <= rhr_rise <= 15 and 0.0 <= sleep_dip <= 2.5):
        raise PatternRejected("recovery-marker move out of physiological caps")

    if trend.category == "adapting":
        if overload_start is not None or peak > 1.1 or hrv_drop > 3 or rhr_rise > 3:
            raise PatternRejected("adapting athlete must have no real overload")
    else:
        if overload_start is None:
            raise PatternRejected(f"{trend.category} athlete needs an overload_start")
        if not (ws + timedelta(days=21) <= overload_start <= we - timedelta(days=7)):
            raise PatternRejected("overload_start leaves no baseline/tail in window")
    return PatternConfig(ws, we, baseline, overload_start, peak,
                         hrv_drop, rhr_rise, sleep_dip)


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise PatternRejected("LLM did not return parseable JSON") from e


def plan_pattern(
    trend: AthleteTrend, *, llm: Callable[[str], str] | None,
) -> PatternConfig:
    """LLM plans the pattern; on any rejection fall back to the deterministic
    config so a single bad response never aborts a whole-roster build."""
    if llm is None:
        return default_pattern(trend)
    try:
        return validate_pattern(_extract_json(llm(_build_prompt(trend))), trend)
    except PatternRejected as e:
        logger.warning("pattern for %s rejected (%s) — using default",
                       trend.athlete_id, e)
        return default_pattern(trend)


# --------------------------------------------------------------------------
# Step 3: deterministic expansion of a validated config into daily data
# --------------------------------------------------------------------------

def _seed(athlete_id: str) -> int:
    return int.from_bytes(hashlib.sha256(athlete_id.encode()).digest()[:4], "big")


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _overload_t(cfg: PatternConfig, d: date) -> float:
    """0 before/at overload_start, ramping to 1 at window_end."""
    if cfg.overload_start is None or d < cfg.overload_start:
        return 0.0
    span = (cfg.window_end - cfg.overload_start).days or 1
    return (d - cfg.overload_start).days / span


def simulate(
    athlete_id: str, cfg: PatternConfig,
) -> tuple[list[Activity], list[WellnessDay]]:
    """Expand the (validated) pattern config into a lean daily feed. Deterministic
    per athlete: same id + config -> identical output."""
    import random
    rng = random.Random(_seed(athlete_id))
    # Per-athlete baselines so each report reads individually.
    hrv0 = 62.0 + rng.uniform(-4, 4)
    rhr0 = 46.0 + rng.uniform(-3, 3)
    pace0 = 8.5 + rng.uniform(-0.3, 0.4)

    def load_factor(d: date) -> float:
        t = _overload_t(cfg, d)
        if cfg.overload_start is None:
            # gentle, healthy progression across the window
            prog = (d - cfg.window_start).days / max(1, (cfg.window_end - cfg.window_start).days)
            return cfg.baseline_load * _lerp(0.95, 1.05, prog)
        return cfg.baseline_load * _lerp(1.0, cfg.overload_peak_factor, t)

    def run(d: date, miles: float, label: str, hour: int, seq: int) -> Activity:
        pace = pace0 + rng.uniform(-0.15, 0.15)
        mi = round(miles * load_factor(d), 2)
        secs = round(mi * pace * 60)
        # HR drifts up with the overload (decoupling), flat otherwise.
        hr = 150.0 + 14.0 * _overload_t(cfg, d) + rng.uniform(-1.5, 1.5)
        return Activity(
            activity_id=f"sim-{athlete_id}-{seq:04d}", source=Source.STRAVA_API,
            athlete_id=athlete_id,
            start_local=datetime.combine(d, time(hour, rng.randint(0, 59))),
            local_date=d, name=label, sport=Sport.RUN,
            moving_time_sec=float(secs), distance_mi=mi,
            avg_speed_mph=round(60.0 / pace, 2), avg_hr=round(hr, 1),
            max_hr=round(hr + rng.uniform(8, 16), 1),
            avg_cadence=round(rng.uniform(84, 90), 1),
            suffer_score=round(secs / 60 * (hr / 150), 1))

    def bike(d: date, minutes: float, label: str, hour: int, seq: int) -> Activity:
        mins = minutes * load_factor(d)
        hr = rng.uniform(124, 136)
        speed = rng.uniform(17, 20)
        return Activity(
            activity_id=f"sim-{athlete_id}-{seq:04d}", source=Source.STRAVA_API,
            athlete_id=athlete_id,
            start_local=datetime.combine(d, time(hour, rng.randint(0, 59))),
            local_date=d, name=label, sport=Sport.RIDE,
            moving_time_sec=round(mins * 60),
            distance_mi=round(mins / 60 * speed, 2), avg_speed_mph=round(speed, 2),
            avg_hr=round(hr, 1), max_hr=round(hr + rng.uniform(10, 20), 1),
            avg_watts=round(rng.uniform(140, 175), 1),
            suffer_score=round(mins * 0.8, 1))

    def lift(d: date, minutes: float, label: str, hour: int, seq: int) -> Activity:
        mins = minutes * load_factor(d)
        return Activity(
            activity_id=f"sim-{athlete_id}-{seq:04d}", source=Source.STRAVA_API,
            athlete_id=athlete_id,
            start_local=datetime.combine(d, time(hour, rng.randint(0, 59))),
            local_date=d, name=label, sport=Sport.STRENGTH,
            moving_time_sec=round(mins * 60), distance_mi=0.0,
            suffer_score=round(mins * 0.5, 1))

    def sessions(d: date, seq: int) -> list[Activity]:
        wd = d.weekday()  # Mon=0
        if wd == 0:
            return [run(d, 4.0, "Morning shakeout", 6, seq)]
        if wd == 1:
            return [bike(d, 60, "Aerobic spin", 17, seq)]
        if wd == 2:
            return [run(d, 5.0, "Tempo run", 7, seq), lift(d, 45, "Lift", 16, seq + 1)]
        if wd == 3:
            return [bike(d, 75, "Long aerobic ride", 9, seq)]
        if wd == 4:
            return []  # rest
        if wd == 5:
            return [run(d, 7.0, "Long run", 8, seq)]
        return [lift(d, 50, "Strength + core", 10, seq)]  # Sun

    def hrv(d: date) -> float:
        return round(hrv0 - cfg.hrv_drop * _overload_t(cfg, d) + rng.uniform(-1.5, 1.5), 1)

    def rhr(d: date) -> float:
        return round(rhr0 + cfg.rhr_rise * _overload_t(cfg, d) + rng.uniform(-1, 1), 1)

    def sleep(d: date) -> float:
        return round(7.7 - cfg.sleep_dip * _overload_t(cfg, d) + rng.uniform(-0.3, 0.3), 1)

    activities: list[Activity] = []
    wellness: list[WellnessDay] = []
    seq, d = 1, cfg.window_start
    while d <= cfg.window_end:
        for a in sessions(d, seq):
            activities.append(a)
            seq += 1
        wellness.append(WellnessDay(
            local_date=d, athlete_id=athlete_id,
            asleep_hours=sleep(d), in_bed_hours=round(sleep(d) + rng.uniform(0.3, 0.8), 1),
            rhr=rhr(d), hrv=hrv(d),
            body_weight_lb=round(150 + rng.uniform(-1.5, 1.5), 1)))
        d += timedelta(days=1)
    return activities, wellness


# --------------------------------------------------------------------------
# encrypted, per-athlete pattern-config cache (one call per athlete at most)
# --------------------------------------------------------------------------

def _cache_path(token_dir: Path) -> Path:
    return Path(token_dir) / "pattern_configs.enc"


def _load_configs(path: Path, key: bytes) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(crypto.decrypt(path.read_bytes(), key))
    except Exception:
        return {}


def _save_configs(path: Path, key: bytes, configs: dict[str, dict]) -> None:
    blob = crypto.encrypt(json.dumps(configs).encode(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)


def _config_to_json(cfg: PatternConfig) -> dict:
    d = asdict(cfg)
    for k in ("window_start", "window_end", "overload_start"):
        d[k] = d[k].isoformat() if d[k] is not None else None
    return d


def _config_from_json(d: dict) -> PatternConfig:
    return PatternConfig(
        window_start=_parse_date(d["window_start"]),
        window_end=_parse_date(d["window_end"]),
        baseline_load=d["baseline_load"],
        overload_start=_parse_date(d["overload_start"]) if d["overload_start"] else None,
        overload_peak_factor=d["overload_peak_factor"],
        hrv_drop=d["hrv_drop"], rhr_rise=d["rhr_rise"], sleep_dip=d["sleep_dip"])


# --------------------------------------------------------------------------
# the harness: build one combined DB for every athlete, then analyze it
# --------------------------------------------------------------------------

@dataclass
class AthleteResult:
    athlete_id: str
    category: str
    n_erg: int
    n_sim_activities: int
    n_erg_anoms: int        # erg split regression/plateau (from the real sheet)
    n_train_anoms: int      # ACWR / load / wellness / pace — the planted signal

    @property
    def n_anomalies(self) -> int:
        return self.n_erg_anoms + self.n_train_anoms


class MultiAthleteHarness:
    """Drives the unchanged pipeline across the whole roster.

    `erg_provider(athlete_id) -> list[Activity]` yields one athlete's real erg
    sessions (wired to ingest.rowing.ingest_rowing by the entry point; injectable
    for offline tests). `llm` is the pattern planner (None -> deterministic).
    """

    def __init__(self, athlete_ids: list[str], erg_provider: Callable[[str], list[Activity]],
                 *, llm: Callable[[str], str] | None = None,
                 cache_key: bytes | None = None, token_dir: Path | None = None):
        self.athlete_ids = athlete_ids
        self.erg_provider = erg_provider
        self.llm = llm
        self.cache_key = cache_key
        self.token_dir = token_dir

    def _plan_cached(self, trend: AthleteTrend, cache: dict[str, dict]) -> PatternConfig:
        if self.cache_key is not None:
            hit = cache.get(trend.fingerprint())
            if hit is not None:
                return _config_from_json(hit)
        cfg = plan_pattern(trend, llm=self.llm)
        if self.cache_key is not None:
            cache[trend.fingerprint()] = _config_to_json(cfg)
        return cfg

    def build(self, db_path: str | Path) -> list[AthleteResult]:
        db_path = Path(db_path)
        if db_path.exists():
            db_path.unlink()
        conn = db.connect(db_path)
        db.init_db(conn)

        cache: dict[str, dict] = {}
        cache_file = None
        if self.cache_key is not None and self.token_dir is not None:
            cache_file = _cache_path(self.token_dir)
            cache = _load_configs(cache_file, self.cache_key)

        all_acts: list[Activity] = []
        all_well: list[WellnessDay] = []
        results: list[AthleteResult] = []
        for cid in self.athlete_ids:
            erg = self.erg_provider(cid)
            trend = classify_trend(erg)
            if trend is None:
                logger.info("skipping %s: not enough comparable erg tests", cid)
                continue
            cfg = self._plan_cached(trend, cache)
            sim_acts, sim_well = simulate(cid, cfg)
            # Only erg tests INSIDE the lean window — the trend was classified on
            # the full season, but mixing far-back tests with a short Strava block
            # would leave a near-empty chronic baseline and spike ACWR for all.
            erg_in_window = [a for a in erg
                             if cfg.window_start <= a.local_date <= cfg.window_end]
            all_acts.extend(erg_in_window)
            all_acts.extend(sim_acts)
            all_well.extend(sim_well)
            results.append(AthleteResult(cid, trend.category, len(erg_in_window),
                                         len(sim_acts), 0, 0))

        # PLAINTEXT (key=None): committed test artifact must be portable.
        db.upsert_activities(conn, all_acts, key=None)
        db.upsert_wellness(conn, all_well, key=None)

        daily_rows = build_daily_rows(all_acts, all_well)
        metrics = compute_metrics(daily_rows)
        anomalies = detect_anomalies(daily_rows, metrics) + detect_erg_anomalies(all_acts)
        db.upsert_metrics(conn, metrics)
        db.upsert_anomalies(conn, anomalies)
        conn.commit()

        if cache_file is not None:
            _save_configs(cache_file, self.cache_key, cache)

        # attribute anomaly counts back per athlete (anomaly_id is "<id>:<date>:<metric>")
        erg_c, train_c = _count_anomalies_by_athlete(conn)
        for r in results:
            r.n_erg_anoms = erg_c.get(r.athlete_id, 0)
            r.n_train_anoms = train_c.get(r.athlete_id, 0)
        return results


_ERG_METRICS = {"erg_split_regression", "erg_split_plateau"}


def _count_anomalies_by_athlete(
    conn: sqlite3.Connection,
) -> tuple[dict[str, int], dict[str, int]]:
    """Per-athlete (erg_anoms, training_anoms). Athlete id is the anomaly_id head."""
    erg_c: dict[str, int] = defaultdict(int)
    train_c: dict[str, int] = defaultdict(int)
    for anomaly_id, metric in conn.execute("SELECT anomaly_id, metric FROM anomaly"):
        head = anomaly_id.split(":", 1)[0]
        (erg_c if metric in _ERG_METRICS else train_c)[head] += 1
    return erg_c, train_c
