"""Driver hours foundation: clock points, activity switching (only one
thing active at a time), and raw hour totals."""
from datetime import datetime, timedelta, timezone

from app import crud, models


def test_clock_point_crud(db):
    point = crud.create_clock_point(db, "Yard Entrance")
    db.commit()
    assert point.token  # a real token was generated
    assert crud.get_clock_point_by_token(db, point.token).clock_point_id == point.clock_point_id

    points = crud.list_clock_points(db)
    assert len(points) == 1

    crud.deactivate_clock_point(db, point.clock_point_id)
    db.commit()
    assert crud.list_clock_points(db) == []
    assert crud.get_clock_point_by_token(db, point.token) is not None  # token still resolves — just not "active"


def test_starting_an_activity_closes_the_previous_one(db):
    """A driver is only ever doing one thing at a time — starting Yard Work
    while Driving is open must close the driving entry, not run both."""
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    driving = crud.start_activity(db, driver.user_id, "Driving")
    db.commit()
    assert driving.ended_at is None

    yard = crud.start_activity(db, driver.user_id, "Yard Work")
    db.commit()

    db.refresh(driving)
    assert driving.ended_at is not None  # closed automatically
    assert yard.ended_at is None
    assert crud.get_active_time_entry(db, driver.user_id).entry_id == yard.entry_id


def test_clock_out_ends_with_nothing_new_started(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.start_activity(db, driver.user_id, "Driving")
    db.commit()

    crud.clock_out(db, driver.user_id)
    db.commit()
    assert crud.get_active_time_entry(db, driver.user_id) is None


def test_clock_out_with_nothing_active_is_harmless(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    result = crud.clock_out(db, driver.user_id)  # nothing open — should not raise
    assert result is None


def test_invalid_activity_type_rejected(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    try:
        crud.start_activity(db, driver.user_id, "Nap Time")
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_hours_summary_totals_by_activity_type(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    now = datetime.now(timezone.utc)
    e1 = crud.start_activity(db, driver.user_id, "Driving")
    e1.started_at = now - timedelta(hours=3)
    e1.ended_at = now - timedelta(hours=1)
    e2 = crud.start_activity(db, driver.user_id, "Yard Work")
    e2.started_at = now - timedelta(hours=1)
    e2.ended_at = now - timedelta(minutes=30)
    db.commit()

    entries = crud.time_entries_for_driver(db, driver.user_id,
                                            (now - timedelta(days=1)).date().isoformat(),
                                            (now + timedelta(days=1)).date().isoformat())
    summary = crud.hours_summary(entries)
    assert summary["Driving"] == 2.0
    assert summary["Yard Work"] == 0.5


def test_hours_summary_counts_still_active_entry_up_to_now(db):
    """An entry with no ended_at yet (driver's still clocked in) should
    still contribute its elapsed time, not be silently skipped."""
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    entry = crud.start_activity(db, driver.user_id, "Driving")
    entry.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    summary = crud.hours_summary([entry])
    assert summary["Driving"] >= 0.9  # roughly an hour, allowing for test execution time


def test_list_all_drivers_time_status(db):
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    crud.start_activity(db, dan.user_id, "Driving")
    db.commit()

    status = crud.list_all_drivers_time_status(db)
    by_name = {s["driver"].full_name: s for s in status}
    assert by_name["Dan Driver"]["active_entry"] is not None
    assert by_name["Sam Driver"]["active_entry"] is None


def test_save_tachograph_record_creates_and_reads_back(db):
    from datetime import date as date_cls
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.save_tachograph_record(
        db, driver.user_id, date_cls(2026, 9, 1), Decimal("7.5"), "admin",
        notes="Read from chart", source_reference="CARD-1234",
    )
    db.commit()

    records = crud.tachograph_records_for_driver(db, driver.user_id, "2026-09-01", "2026-09-01")
    assert len(records) == 1
    assert records[0].driving_hours == Decimal("7.50")
    assert records[0].source_reference == "CARD-1234"
    assert records[0].entered_by == "admin"


def test_save_tachograph_record_same_day_updates_not_duplicates(db):
    """Re-entering the same driver+date (e.g. a correction) must update
    the existing record, not create a second one for that day."""
    from datetime import date as date_cls
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.save_tachograph_record(db, driver.user_id, date_cls(2026, 9, 1), Decimal("7.0"), "admin")
    db.commit()
    crud.save_tachograph_record(db, driver.user_id, date_cls(2026, 9, 1), Decimal("7.5"), "admin",
                                 notes="Corrected after re-checking chart")
    db.commit()

    records = crud.tachograph_records_for_driver(db, driver.user_id, "2026-09-01", "2026-09-01")
    assert len(records) == 1
    assert records[0].driving_hours == Decimal("7.50")
    assert records[0].notes == "Corrected after re-checking chart"


def test_delete_tachograph_record(db):
    from datetime import date as date_cls
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    record = crud.save_tachograph_record(db, driver.user_id, date_cls(2026, 9, 1), Decimal("7.5"), "admin")
    db.commit()

    crud.delete_tachograph_record(db, record.record_id)
    db.commit()
    assert crud.tachograph_records_for_driver(db, driver.user_id, "2026-09-01", "2026-09-01") == []


def test_tachograph_records_are_per_driver(db):
    """Verified hours entered for one driver must never show up under another."""
    from datetime import date as date_cls
    from decimal import Decimal
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    crud.save_tachograph_record(db, dan.user_id, date_cls(2026, 9, 1), Decimal("7.5"), "admin")
    db.commit()

    assert len(crud.tachograph_records_for_driver(db, dan.user_id, "2026-09-01", "2026-09-01")) == 1
    assert len(crud.tachograph_records_for_driver(db, sam.user_id, "2026-09-01", "2026-09-01")) == 0


def test_vehicle_gets_a_qr_token_on_creation(db):
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()
    assert vehicle.qr_token
    assert crud.get_vehicle_by_qr_token(db, vehicle.qr_token).vehicle_id == vehicle.vehicle_id


def test_backfill_gives_tokens_to_vehicles_missing_one(db):
    """Simulates a vehicle that existed before this feature — created with
    no token, then backfilled on the next app startup."""
    vehicle = models.Vehicle(registration="OLD01", qr_token=None)
    db.add(vehicle)
    db.commit()
    assert vehicle.qr_token is None

    crud.backfill_vehicle_qr_tokens(db)
    db.commit()
    db.refresh(vehicle)
    assert vehicle.qr_token is not None


def test_scanning_cab_qr_starts_driving_that_vehicle(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    entry = crud.start_driving_vehicle(db, driver.user_id, vehicle.vehicle_id)
    db.commit()

    assert entry.activity_type == "Driving"
    assert entry.vehicle_id == vehicle.vehicle_id
    active = crud.get_active_driver_for_vehicle(db, vehicle.vehicle_id)
    assert active.driver_user_id == driver.user_id


def test_scanning_cab_qr_hands_over_from_the_previous_driver(db):
    """The critical scenario: Dan's driving TC01, Sam scans TC01's QR for
    a last-minute swap — Dan's entry must close automatically, and TC01
    must never appear to have two active drivers at once."""
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    dan_entry = crud.start_driving_vehicle(db, dan.user_id, vehicle.vehicle_id)
    db.commit()
    assert dan_entry.ended_at is None

    sam_entry = crud.start_driving_vehicle(db, sam.user_id, vehicle.vehicle_id)
    db.commit()

    db.refresh(dan_entry)
    assert dan_entry.ended_at is not None  # closed automatically by the handover
    assert sam_entry.ended_at is None

    active = crud.get_active_driver_for_vehicle(db, vehicle.vehicle_id)
    assert active.driver_user_id == sam.user_id  # exactly one driver shows as active, and it's Sam

    # Dan's own active entry (for anything) is also correctly nothing now
    assert crud.get_active_time_entry(db, dan.user_id) is None


def test_handover_does_not_affect_other_vehicles(db):
    """Dan driving TC02 must be completely unaffected by a handover on TC01."""
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    tc01 = crud.save_vehicle(db, "TC01")
    tc02 = crud.save_vehicle(db, "TC02")
    db.commit()

    crud.start_driving_vehicle(db, dan.user_id, tc02.vehicle_id)
    db.commit()

    crud.start_driving_vehicle(db, sam.user_id, tc01.vehicle_id)
    db.commit()

    dan_active = crud.get_active_driver_for_vehicle(db, tc02.vehicle_id)
    assert dan_active is not None
    assert dan_active.driver_user_id == dan.user_id


def test_same_driver_rescanning_same_vehicle_does_not_duplicate(db):
    """A driver scanning their own vehicle's QR twice (e.g. by mistake)
    shouldn't create two overlapping entries."""
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    first = crud.start_driving_vehicle(db, driver.user_id, vehicle.vehicle_id)
    db.commit()
    second = crud.start_driving_vehicle(db, driver.user_id, vehicle.vehicle_id)
    db.commit()

    db.refresh(first)
    assert first.ended_at is not None  # closed by their own re-scan (via start_activity's own-driver close)
    assert second.ended_at is None
    active = crud.get_active_driver_for_vehicle(db, vehicle.vehicle_id)
    assert active.entry_id == second.entry_id
