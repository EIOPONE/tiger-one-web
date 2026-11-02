"""15-minute timetable: placing, moving and resizing jobs in a driver's
day, with overlaps refused."""
from datetime import date

import pytest

from app import crud
from tests.test_kanban import _confirmed_order

DAY = date(2026, 9, 10)


def _setup(db, n=1):
    orders = [_confirmed_order(db, suffix=str(i)) for i in range(n)]
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    return orders, driver


def test_schedule_order_at_slot_defaults_to_two_hours(db):
    (order,), driver = _setup(db)
    d = crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 9 * 60)
    db.commit()
    assert (d.start_minutes, d.duration_minutes, d.driver_user_id) == (540, 120, driver.user_id)


def test_overlapping_job_for_same_driver_is_refused(db):
    orders, driver = _setup(db, 2)
    crud.schedule_order_at_slot(db, orders[0].order_id, driver.user_id, DAY, 9 * 60)
    db.commit()
    with pytest.raises(ValueError, match="Clashes"):
        crud.schedule_order_at_slot(db, orders[1].order_id, driver.user_id, DAY, 10 * 60)
    # back-to-back is fine
    crud.schedule_order_at_slot(db, orders[1].order_id, driver.user_id, DAY, 11 * 60)


def test_different_drivers_can_overlap(db):
    orders, dan = _setup(db, 2)
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    crud.schedule_order_at_slot(db, orders[0].order_id, dan.user_id, DAY, 9 * 60)
    crud.schedule_order_at_slot(db, orders[1].order_id, sam.user_id, DAY, 9 * 60)


def test_slot_rules_15_minute_blocks_and_working_window(db):
    (order,), driver = _setup(db)
    with pytest.raises(ValueError):
        crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 9 * 60 + 10)
    with pytest.raises(ValueError):
        crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 7 * 60)
    with pytest.raises(ValueError):
        crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 19 * 60)  # 2h would end 21:00
    crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 18 * 60)  # ends exactly 20:00


def test_resize_and_move_existing_delivery(db):
    (order,), driver = _setup(db)
    d = crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 9 * 60)
    db.commit()
    crud.place_delivery(db, d.delivery_id, driver.user_id, 9 * 60, 45)
    assert d.duration_minutes == 45
    crud.place_delivery(db, d.delivery_id, driver.user_id, 13 * 60 + 15)
    assert (d.start_minutes, d.duration_minutes) == (13 * 60 + 15, 45)


def test_resize_into_next_job_refused(db):
    orders, driver = _setup(db, 2)
    a = crud.schedule_order_at_slot(db, orders[0].order_id, driver.user_id, DAY, 9 * 60)
    crud.schedule_order_at_slot(db, orders[1].order_id, driver.user_id, DAY, 11 * 60)
    with pytest.raises(ValueError, match="Clashes"):
        crud.place_delivery(db, a.delivery_id, driver.user_id, 9 * 60, 180)


def test_moving_to_another_driver_and_unassign_clear_time_rules(db):
    (order,), dan = _setup(db)
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    d = crud.schedule_order_at_slot(db, order.order_id, dan.user_id, DAY, 9 * 60)
    crud.place_delivery(db, d.delivery_id, sam.user_id, 10 * 60)
    assert (d.driver_user_id, d.start_minutes) == (sam.user_id, 600)
    crud.unassign_delivery(db, d.delivery_id)
    assert d.start_minutes is None and d.driver_user_id is None


def test_driver_list_follows_timetable_order(db):
    orders, driver = _setup(db, 2)
    late = crud.schedule_order_at_slot(db, orders[0].order_id, driver.user_id, DAY, 14 * 60)
    early = crud.schedule_order_at_slot(db, orders[1].order_id, driver.user_id, DAY, 8 * 60)
    db.commit()
    assert [x.delivery_id for x in crud.deliveries_for_driver(db, driver.user_id)] == [early.delivery_id, late.delivery_id]


def test_schedule_entry_add_edit_delete(db):
    _, driver = _setup(db, 0)
    e = crud.save_schedule_entry(db, driver.user_id, DAY, 13 * 60, 90, "Yard tidy", "Yard work", "Sweep up")
    db.commit()
    assert (e.start_minutes, e.duration_minutes, e.category) == (780, 90, "Yard work")
    crud.move_schedule_entry(db, e.entry_id, driver.user_id, 14 * 60, 30)
    assert (e.start_minutes, e.duration_minutes) == (840, 30)
    crud.delete_schedule_entry(db, e.entry_id)
    assert crud.entries_for_date(db, DAY) == []


def test_schedule_entry_needs_a_title_and_unknown_category_becomes_other(db):
    _, driver = _setup(db, 0)
    with pytest.raises(ValueError):
        crud.save_schedule_entry(db, driver.user_id, DAY, 540, 60, "  ")
    e = crud.save_schedule_entry(db, driver.user_id, DAY, 540, 60, "Thing", "Nonsense")
    assert e.category == "Other"


def test_tasks_and_deliveries_cannot_double_book_a_driver(db):
    (order,), driver = _setup(db)
    crud.save_schedule_entry(db, driver.user_id, DAY, 9 * 60, 60, "Mixer service", "Maintenance")
    db.commit()
    with pytest.raises(ValueError, match="Clashes with Mixer service"):
        crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 9 * 60 + 30)
    d = crud.schedule_order_at_slot(db, order.order_id, driver.user_id, DAY, 10 * 60)  # right after: fine
    with pytest.raises(ValueError, match="Clashes"):
        crud.save_schedule_entry(db, driver.user_id, DAY, 11 * 60, 60, "Overlaps job", "Other")
    assert d.start_minutes == 600


def test_driver_sees_their_upcoming_tasks(db):
    _, dan = _setup(db, 0)
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    crud.save_schedule_entry(db, dan.user_id, DAY, 540, 60, "Dan's task", "Other")
    crud.save_schedule_entry(db, sam.user_id, DAY, 540, 60, "Sam's task", "Other")
    db.commit()
    assert [t.title for t in crud.entries_for_driver(db, dan.user_id, DAY)] == ["Dan's task"]
