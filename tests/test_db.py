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
