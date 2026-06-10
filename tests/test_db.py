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


def test_encrypted_columns_are_ciphertext_on_disk(tmp_path):
    import os

    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    key = os.urandom(32)
    a = _sample_activity(
        name="Run past 123 Main St", device_name="Garmin Forerunner"
    )
    db.upsert_activities(conn, [a], key=key)
    # Raw read bypassing decryption: the free-text PII must be ciphertext.
    raw = conn.execute("SELECT name, device_name FROM activity").fetchone()
    assert "123 Main St" not in raw["name"]
    assert raw["name"].startswith("enc:")
    assert "Garmin" not in (raw["device_name"] or "")
    # Numeric columns stay plaintext (queryable / indexable).
    assert conn.execute("SELECT distance_mi FROM activity").fetchone()[0] > 0
    # Round-trips back to plaintext with the key.
    got = db.get_activities(conn, key=key)
    assert got[0].name == "Run past 123 Main St"
    assert got[0].device_name == "Garmin Forerunner"


def test_wrong_key_cannot_read_encrypted_columns(tmp_path):
    import os

    from cryptography.exceptions import InvalidTag

    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    db.upsert_activities(conn, [_sample_activity()], key=os.urandom(32))
    try:
        db.get_activities(conn, key=os.urandom(32))
        raise AssertionError("reading with the wrong key must fail")
    except InvalidTag:
        pass


def test_count_activities(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    assert db.count_activities(conn) == 0
    db.upsert_activities(conn, [_sample_activity()])
    assert db.count_activities(conn) == 1
