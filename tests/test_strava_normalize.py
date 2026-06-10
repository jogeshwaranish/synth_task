from datetime import date, datetime

from schemas import Source, Sport
from ingest.strava import to_activity

# Mirrors real rows A0004 (Run) and A0002 (VirtualRide) from the export, in the
# shape the Strava API returns (SI units, sport_type, Z-suffixed local time).
RUN_RAW = {
    "id": 4,
    "name": "Afternoon Run",
    "sport_type": "Run",
    "type": "Run",
    "start_date_local": "2026-05-13T09:40:31Z",
    "start_date": "2026-05-13T23:40:31Z",
    "trainer": False,
    "moving_time": 3501,
    "elapsed_time": 3504,
    "distance": 9943.6,
    "total_elevation_gain": 281.0,
    "average_speed": 2.84,
    "average_heartrate": 147.9,
    "max_heartrate": 169,
}
RIDE_RAW = {
    "id": 2,
    "name": "Zwift - Tempus Fugit in Watopia",
    "sport_type": "VirtualRide",
    "start_date_local": "2026-05-14T03:04:25Z",
    "start_date": "2026-05-14T17:04:25Z",
    "trainer": False,
    "moving_time": 3494,
    "elapsed_time": 3494,
    "distance": 32568.4,
    "total_elevation_gain": 52.0,
    "average_speed": 9.321,
    "average_watts": 129.5,
    "weighted_average_watts": 131,
    "kilojoules": 452.6,
}


def test_run_normalizes_units_and_local_date():
    a = to_activity(RUN_RAW, athlete_id="basil")
    assert a.activity_id == "4"
    assert a.source == Source.STRAVA_API
    assert a.athlete_id == "basil"
    assert a.sport == Sport.RUN
    assert a.start_local == datetime(2026, 5, 13, 9, 40, 31)
    assert a.local_date == date(2026, 5, 13)  # from LOCAL time, not UTC
    assert abs(a.distance_mi - 6.17866) < 1e-3       # 9943.6 m
    assert abs(a.elevation_gain_ft - 921.916) < 1e-2  # 281 m
    assert abs(a.avg_speed_mph - 6.35291) < 1e-3      # 2.84 m/s
    assert a.avg_hr == 147.9


def test_late_night_local_date_belongs_to_local_day():
    # 11:58 PM local on the 13th is UTC the 14th — must stay the 13th.
    raw = {**RUN_RAW, "start_date_local": "2026-05-13T23:58:00Z",
           "start_date": "2026-05-14T13:58:00Z"}
    a = to_activity(raw, athlete_id="basil")
    assert a.local_date == date(2026, 5, 13)


def test_unknown_sport_falls_back_to_other():
    a = to_activity({**RUN_RAW, "sport_type": "Pickleball"}, athlete_id="basil")
    assert a.sport == Sport.OTHER


def test_ride_carries_power_fields():
    a = to_activity(RIDE_RAW, athlete_id="basil")
    assert a.sport == Sport.VIRTUAL_RIDE
    assert a.avg_watts == 129.5
    assert a.weighted_watts == 131
    assert a.avg_hr is None  # absent in raw -> None, not dropped
