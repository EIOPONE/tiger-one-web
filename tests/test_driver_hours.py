"""Driver hours foundation: clock points, activity switching (only one
thing active at a time), and raw hour totals."""
from datetime import date, datetime, timedelta, timezone

import pytest

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


def test_finish_work_records_driving_hours_and_clocks_out(db):
    """The core new behaviour: finishing work in one action both saves
    today's self-reported driving hours and clocks the driver out."""
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.start_activity(db, driver.user_id, "On Shift")
    db.commit()
    assert crud.get_active_time_entry(db, driver.user_id) is not None

    crud.finish_work(db, driver.user_id, Decimal("4.0"))
    db.commit()

    assert crud.get_active_time_entry(db, driver.user_id) is None  # clocked out
    assert crud.driver_has_entered_tacho_today(db, driver.user_id) is True
    today_records = crud.tachograph_records_for_driver(
        db, driver.user_id, datetime.now(timezone.utc).date().isoformat(), datetime.now(timezone.utc).date().isoformat(),
    )
    assert today_records[0].driving_hours == Decimal("4.00")
    assert today_records[0].entered_by == "driver (self-reported)"


def test_driver_has_entered_tacho_today_before_and_after(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    assert crud.driver_has_entered_tacho_today(db, driver.user_id) is False

    from decimal import Decimal
    crud.finish_work(db, driver.user_id, Decimal("0"))
    db.commit()
    assert crud.driver_has_entered_tacho_today(db, driver.user_id) is True


def test_finish_work_allows_zero_driving_hours(db):
    """A pure yard-work day with no driving at all — 0 must be accepted,
    not treated as 'no answer given'."""
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.start_activity(db, driver.user_id, "On Shift")
    db.commit()

    crud.finish_work(db, driver.user_id, Decimal("0"))
    db.commit()
    assert crud.get_active_time_entry(db, driver.user_id) is None
    assert crud.driver_has_entered_tacho_today(db, driver.user_id) is True


def test_shift_hours_summary_uses_on_shift_bucket(db):
    """The simplified QR flow only ever creates 'On Shift' entries now —
    confirms the total shows up under that bucket for the timesheet KPI."""
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    entry = crud.start_activity(db, driver.user_id, "On Shift")
    entry.started_at = datetime.now(timezone.utc) - timedelta(hours=9, minutes=30)
    db.commit()
    crud.finish_work(db, driver.user_id, Decimal("4.0"))
    db.commit()

    entries = crud.time_entries_for_driver(db, driver.user_id,
                                            (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
                                            (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())
    summary = crud.hours_summary(entries)
    assert summary["On Shift"] == pytest.approx(9.5, abs=0.05)


def test_add_holiday_is_idempotent(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    h1 = crud.add_holiday(db, driver.user_id, date(2026, 9, 10), "admin")
    db.commit()
    h2 = crud.add_holiday(db, driver.user_id, date(2026, 9, 10), "admin")
    db.commit()
    assert h1.holiday_id == h2.holiday_id  # no duplicate created

    holidays = crud.holidays_for_driver(db, driver.user_id, "2026-09-01", "2026-09-30")
    assert len(holidays) == 1


def test_remove_holiday(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    holiday = crud.add_holiday(db, driver.user_id, date(2026, 9, 10), "admin")
    db.commit()

    crud.remove_holiday(db, holiday.holiday_id)
    db.commit()
    assert crud.holidays_for_driver(db, driver.user_id, "2026-09-01", "2026-09-30") == []


def test_holiday_day_shows_zero_hours_even_with_a_real_shift_that_day(db):
    """The actual fix this was built for: a day marked HOLIDAY must show
    zero hours — even if, for whatever reason, there's also shift or tacho
    data recorded for that same date. Holiday always wins."""
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    # A genuine 8-hour shift logged on 10 Sept...
    entry = crud.start_activity(db, driver.user_id, "On Shift")
    entry.started_at = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
    entry.ended_at = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)
    db.commit()
    crud.save_tachograph_record(db, driver.user_id, date(2026, 9, 10), Decimal("5.0"), "admin")
    db.commit()

    # ...but the office has since marked that day as a holiday.
    crud.add_holiday(db, driver.user_id, date(2026, 9, 10), "admin", notes="Booked leave")
    db.commit()

    days = crud.daily_timesheet(db, driver.user_id, "2026-09-08", "2026-09-12")
    holiday_day = next(d for d in days if d["date"] == date(2026, 9, 10))
    assert holiday_day["is_holiday"] is True
    assert holiday_day["hours_worked"] == 0.0
    assert holiday_day["driving_hours"] == 0.0
    assert holiday_day["worked"] is False


def test_daily_timesheet_totals_exclude_holidays_correctly(db):
    """Two worked days plus one holiday — the holiday must contribute
    nothing to either total, proving the WTD-skew problem is actually fixed."""
    from decimal import Decimal
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    for day_num, hours in [(8, 8), (9, 7.5)]:
        entry = crud.start_activity(db, driver.user_id, "On Shift")
        entry.started_at = datetime(2026, 9, day_num, 8, 0, tzinfo=timezone.utc)
        entry.ended_at = datetime(2026, 9, day_num, 8 + int(hours), int((hours % 1) * 60), tzinfo=timezone.utc)
        db.commit()
        crud.save_tachograph_record(db, driver.user_id, date(2026, 9, day_num), Decimal("4.0"), "driver (self-reported)")
        db.commit()

    crud.add_holiday(db, driver.user_id, date(2026, 9, 10), "admin")
    db.commit()

    days = crud.daily_timesheet(db, driver.user_id, "2026-09-08", "2026-09-10")
    totals = crud.daily_timesheet_totals(days)
    assert totals["total_hours_worked"] == pytest.approx(15.5, abs=0.05)  # 8 + 7.5, holiday contributes 0
    assert totals["total_driving_hours"] == pytest.approx(8.0, abs=0.05)  # 4 + 4, holiday contributes 0
    assert totals["holiday_days"] == 1


def test_daily_timesheet_rest_day_shown_but_not_worked(db):
    """A day with no shift and no holiday marked — must show as a rest
    day, not silently disappear or get counted as anything."""
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    days = crud.daily_timesheet(db, driver.user_id, "2026-09-08", "2026-09-08")
    assert len(days) == 1
    assert days[0]["worked"] is False
    assert days[0]["is_holiday"] is False
    assert days[0]["hours_worked"] == 0.0
