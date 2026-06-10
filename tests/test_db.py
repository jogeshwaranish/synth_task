from datetime import date, datetime

from schemas import Activity, Source, Sport
from store import db


def _sample_activity(**over) -> Activity:
    base = dict(
        activity_id="A0004",
        source=Source.STRAVA_API,
        athlete_id="basil",
        start_local=datetime(2026, 5, 13, 9, 40, 31),
        start_utc=datetime(2026, 5, 13, 23, 40, 31),
        local_date=date(2026, 5, 13),
        name="Afternoon Run",
        sport=Sport.RUN,
        is_trainer=False,
        moving_time_sec=3501.0,
        distance_mi=6.178664676,
        elevation_gain_ft=921.91604,
        avg_speed_mph=6.3529096,
        avg_hr=147.9,
        max_hr=169.0,
    )
    base.update(over)
    return Activity(**base)


def test_upsert_then_read_roundtrips(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    a = _sample_activity()
    n = db.upsert_activities(conn, [a])
    assert n == 1
    got = db.get_activities(conn, athlete_id="basil")
    assert got == [a]


def test_upsert_is_idempotent_on_activity_id(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    db.upsert_activities(conn, [_sample_activity(name="old")])
    db.upsert_activities(conn, [_sample_activity(name="new")])
    got = db.get_activities(conn)
    assert len(got) == 1
    assert got[0].name == "new"


def test_is_trainer_true_roundtrips(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    a = _sample_activity(is_trainer=True)
    db.upsert_activities(conn, [a])
    assert db.get_activities(conn)[0].is_trainer is True


def test_all_optional_fields_none_roundtrips(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    a = _sample_activity(
        start_utc=None, elapsed_time_sec=None, elevation_gain_ft=None,
        avg_speed_mph=None, avg_hr=None, max_hr=None, device_name=None,
    )
    db.upsert_activities(conn, [a])
    assert db.get_activities(conn) == [a]


def test_get_activities_filters_by_athlete(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    db.upsert_activities(conn, [
        _sample_activity(activity_id="B1", athlete_id="basil"),
        _sample_activity(activity_id="A1", athlete_id="anish"),
    ])
    basil = db.get_activities(conn, athlete_id="basil")
    assert [a.activity_id for a in basil] == ["B1"]


def test_count_activities(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    assert db.count_activities(conn) == 0
    db.upsert_activities(conn, [_sample_activity()])
    assert db.count_activities(conn) == 1
