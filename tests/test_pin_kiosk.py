"""Base-tablet PIN kiosk: PIN checks with lockout, and driving sessions
layered on top of the shift."""
from datetime import datetime, timedelta, timezone

import pytest

from app import crud, models


@pytest.fixture(autouse=True)
def _clear_lockouts():
    crud._kiosk_failures.clear()
    yield
    crud._kiosk_failures.clear()


def _driver(db, name="Dan Driver", username="dan", pin="4821"):
    d = crud.create_driver(db, name, username, pin)
    db.commit()
    return d


def test_right_pin_passes_wrong_pin_fails(db):
    d = _driver(db)
    assert crud.verify_kiosk_pin(db, d.user_id, "4821").user_id == d.user_id
    with pytest.raises(PermissionError, match="Wrong PIN"):
        crud.verify_kiosk_pin(db, d.user_id, "0000")


def test_five_wrong_pins_lock_the_driver_out_even_for_the_right_pin(db):
    d = _driver(db)
    for _ in range(4):
        with pytest.raises(PermissionError, match="Wrong PIN"):
            crud.verify_kiosk_pin(db, d.user_id, "1111")
    with pytest.raises(PermissionError, match="Too many"):
        crud.verify_kiosk_pin(db, d.user_id, "1111")
    with pytest.raises(PermissionError, match="Too many"):
        crud.verify_kiosk_pin(db, d.user_id, "4821")  # locked, even with the correct PIN


def test_lockout_is_per_driver_and_a_good_pin_resets_the_count(db):
    dan = _driver(db)
    sam = _driver(db, "Sam Driver", "sam", "1234")
    for _ in range(3):
        with pytest.raises(PermissionError):
            crud.verify_kiosk_pin(db, dan.user_id, "9999")
    crud.verify_kiosk_pin(db, dan.user_id, "4821")  # resets
    for _ in range(4):
        with pytest.raises(PermissionError, match="Wrong PIN"):
            crud.verify_kiosk_pin(db, dan.user_id, "9999")
    assert crud.verify_kiosk_pin(db, sam.user_id, "1234")  # unaffected


def test_office_users_and_unknown_ids_cannot_use_the_kiosk(db):
    admin = db.query(models.AppUser).filter_by(username="admin").one()
    with pytest.raises(ValueError):
        crud.verify_kiosk_pin(db, admin.user_id, "tigerone")
    with pytest.raises(ValueError):
        crud.verify_kiosk_pin(db, 9999, "1234")


def test_cannot_start_driving_before_clocking_in(db):
    d = _driver(db)
    with pytest.raises(ValueError, match="Clock in first"):
        crud.start_driving(db, d.user_id)


def test_driving_runs_inside_the_shift_without_closing_it(db):
    d = _driver(db)
    crud.start_activity(db, d.user_id, "On Shift", source="pin_kiosk")
    crud.start_driving(db, d.user_id)
    db.commit()
    assert crud.get_active_time_entry(db, d.user_id).activity_type == "On Shift"  # still clocked in
    assert crud.driver_kiosk_states(db)[d.user_id] == "driving"
    crud.stop_driving(db, d.user_id)
    assert crud.driver_kiosk_states(db)[d.user_id] == "on_shift"


def test_clocking_out_also_ends_driving(db):
    d = _driver(db)
    crud.start_activity(db, d.user_id, "On Shift", source="pin_kiosk")
    crud.start_driving(db, d.user_id)
    crud.finish_work(db, d.user_id, 2.5)
    db.commit()
    assert crud.active_driving_session(db, d.user_id) is None
    assert crud.driver_kiosk_states(db)[d.user_id] == "out"


def test_vehicle_is_handed_over_to_the_next_driver(db):
    dan = _driver(db)
    sam = _driver(db, "Sam Driver", "sam", "1234")
    vehicle = crud.save_vehicle(db, "AB12 CDE", "8-wheel mixer")
    db.commit()
    for d in (dan, sam):
        crud.start_activity(db, d.user_id, "On Shift", source="pin_kiosk")
    crud.start_driving(db, dan.user_id, vehicle.vehicle_id)
    crud.start_driving(db, sam.user_id, vehicle.vehicle_id)
    db.commit()
    assert crud.active_driving_session(db, sam.user_id) is not None
    assert crud.active_driving_session(db, dan.user_id) is None  # handed over


def test_tracked_driving_hours_today_adds_up_sessions(db):
    from datetime import date, time
    d = _driver(db)
    day_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    db.add(models.DrivingSession(driver_user_id=d.user_id, started_at=day_start, ended_at=day_start + timedelta(hours=1)))
    db.add(models.DrivingSession(driver_user_id=d.user_id, started_at=day_start + timedelta(hours=1),
                                 ended_at=day_start + timedelta(hours=1, minutes=30)))
    db.commit()
    assert crud.driving_hours_today(db, d.user_id) == 1.5
