"""Kanban job board: which confirmed orders still need a driver assigned
for a given date, and which deliveries are already scheduled that day."""
from datetime import date

from app import crud


def _confirmed_order(db, requested_date="2026-09-10", suffix=""):
    customer = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": f"CEMENT{suffix}", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": f"C30{suffix}", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    return crud.save_order(db, {
        "customer_id": customer.customer_id, "project": "Test job", "site_address": "1 Test Rd",
        "requested_date": requested_date, "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")


def test_unscheduled_confirmed_orders_shows_up_when_nothing_assigned(db):
    order = _confirmed_order(db, requested_date="2026-09-10")
    db.commit()

    unassigned = crud.unscheduled_confirmed_orders(db, "2026-09-10")
    assert len(unassigned) == 1
    assert unassigned[0].order_id == order.order_id


def test_unscheduled_confirmed_orders_excludes_ones_already_scheduled(db):
    order = _confirmed_order(db, requested_date="2026-09-10")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    unassigned = crud.unscheduled_confirmed_orders(db, "2026-09-10")
    assert unassigned == []


def test_unscheduled_confirmed_orders_excludes_other_dates(db):
    _confirmed_order(db, requested_date="2026-09-11")
    db.commit()
    assert crud.unscheduled_confirmed_orders(db, "2026-09-10") == []


def test_deliveries_for_date_groups_correctly(db):
    order1 = _confirmed_order(db, suffix="1")
    order2 = _confirmed_order(db, suffix="2")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.create_delivery(db, order1.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    crud.create_delivery(db, order2.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 11))
    db.commit()

    day_10 = crud.deliveries_for_date(db, date(2026, 9, 10))
    assert len(day_10) == 1
    assert day_10[0].order_id == order1.order_id


def test_deliveries_for_date_excludes_cancelled(db):
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    delivery.status = "Cancelled"
    db.commit()

    assert crud.deliveries_for_date(db, date(2026, 9, 10)) == []


def test_kanban_drag_to_driver_column_creates_delivery(db):
    """The 'drag an unassigned order onto a driver' action, at the data level."""
    order = _confirmed_order(db, requested_date="2026-09-10")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    assert len(crud.unscheduled_confirmed_orders(db, "2026-09-10")) == 1

    crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    assert crud.unscheduled_confirmed_orders(db, "2026-09-10") == []
    assert len(crud.deliveries_for_date(db, date(2026, 9, 10))) == 1


def test_kanban_drag_between_drivers_reassigns(db):
    """The 'drag a card from one driver's column to another' action."""
    order = _confirmed_order(db, requested_date="2026-09-10")
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=dan.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=sam.user_id, vehicle_id=None)
    db.commit()

    assert delivery.driver_user_id == sam.user_id
    # the driver-side notification endpoint reflects the change immediately
    assert delivery.delivery_id in crud.active_delivery_ids_for_driver(db, sam.user_id)
    assert delivery.delivery_id not in crud.active_delivery_ids_for_driver(db, dan.user_id)


def test_active_delivery_ids_excludes_delivered(db):
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    assert delivery.delivery_id in crud.active_delivery_ids_for_driver(db, driver.user_id)

    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "", None, None)
    db.commit()
    assert delivery.delivery_id not in crud.active_delivery_ids_for_driver(db, driver.user_id)


def test_schedule_without_a_driver_leaves_it_unassigned(db):
    """Office can now schedule a job (date/vehicle known) before it's
    known who's actually doing it — it shows up driverless, not blocked."""
    order = _confirmed_order(db, requested_date="2026-09-10")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, scheduled_date=date(2026, 9, 10))
    db.commit()
    assert delivery.driver_user_id is None
    assert delivery.driver_name == ""

    # It must surface in deliveries_for_date so the kanban board can show
    # it in the Unassigned column, not silently disappear.
    day_deliveries = crud.deliveries_for_date(db, date(2026, 9, 10))
    assert len(day_deliveries) == 1
    assert day_deliveries[0].driver_user_id is None


def test_unassign_delivery_removes_the_driver(db):
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    assert delivery.driver_user_id == driver.user_id

    crud.unassign_delivery(db, delivery.delivery_id)
    db.commit()

    assert delivery.driver_user_id is None
    assert delivery.driver_name == ""
    # gone from that driver's active list, and from the notification snapshot too
    assert delivery.delivery_id not in crud.active_delivery_ids_for_driver(db, driver.user_id)


def test_unassign_then_reassign_round_trip(db):
    """The actual described workflow: unassign a job, it shows up
    unassigned, then it gets picked up by (possibly a different) driver."""
    order = _confirmed_order(db, requested_date="2026-09-10")
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=dan.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    crud.unassign_delivery(db, delivery.delivery_id)
    db.commit()
    assert delivery.driver_user_id is None

    crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=sam.user_id, vehicle_id=None)
    db.commit()
    assert delivery.driver_user_id == sam.user_id
    assert delivery.delivery_id in crud.active_delivery_ids_for_driver(db, sam.user_id)


def test_cannot_unassign_a_delivered_run(db):
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "", None, None)
    db.commit()

    try:
        crud.unassign_delivery(db, delivery.delivery_id)
        assert False, "expected a ValueError"
    except ValueError:
        pass
    assert delivery.driver_user_id == driver.user_id  # unchanged


def test_scheduling_captures_vehicle(db):
    """The bug this session fixed: scheduling via the kanban drag used to
    silently drop the vehicle. Confirms create_delivery still saves one
    when given, and it's visible on the resulting board data."""
    order = _confirmed_order(db, requested_date="2026-09-10")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    delivery = crud.create_delivery(
        db, order.order_id, driver_user_id=driver.user_id,
        vehicle_id=vehicle.vehicle_id, scheduled_date=date(2026, 9, 10),
    )
    db.commit()
    assert delivery.vehicle_id == vehicle.vehicle_id
    assert delivery.vehicle == "TC01"


def test_set_vehicle_via_kanban_card_does_not_touch_driver(db):
    """The new per-card vehicle picker — must only change the vehicle,
    never accidentally reassign the driver (reuses reassign_delivery
    with driver_user_id=None, which must be a true no-op on the driver)."""
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    assert delivery.vehicle_id is None

    crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=None, vehicle_id=vehicle.vehicle_id)
    db.commit()

    assert delivery.vehicle_id == vehicle.vehicle_id
    assert delivery.driver_user_id == driver.user_id  # unchanged


def test_new_jobs_land_at_the_end_of_a_drivers_queue(db):
    """A driver's second job of the day must default to after the first,
    not overwrite or randomly interleave with it."""
    order1 = _confirmed_order(db, suffix="A", requested_date="2026-09-10")
    order2 = _confirmed_order(db, suffix="B", requested_date="2026-09-10")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    d1 = crud.create_delivery(db, order1.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    d2 = crud.create_delivery(db, order2.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    assert d2.sequence > d1.sequence
    jobs = crud.deliveries_for_driver(db, driver.user_id)
    assert [j.delivery_id for j in jobs] == [d1.delivery_id, d2.delivery_id]


def test_reorder_deliveries_changes_driver_view_order(db):
    """The actual described need: office sets a priority order, the
    driver's own job list follows it."""
    order1 = _confirmed_order(db, suffix="A", requested_date="2026-09-10")
    order2 = _confirmed_order(db, suffix="B", requested_date="2026-09-10")
    order3 = _confirmed_order(db, suffix="C", requested_date="2026-09-10")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    d1 = crud.create_delivery(db, order1.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    d2 = crud.create_delivery(db, order2.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    d3 = crud.create_delivery(db, order3.order_id, driver_user_id=driver.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()
    assert [j.delivery_id for j in crud.deliveries_for_driver(db, driver.user_id)] == [d1.delivery_id, d2.delivery_id, d3.delivery_id]

    # Office decides job 3 is actually the most urgent, reorders: 3, 1, 2
    crud.reorder_deliveries(db, [d3.delivery_id, d1.delivery_id, d2.delivery_id])
    db.commit()

    jobs = crud.deliveries_for_driver(db, driver.user_id)
    assert [j.delivery_id for j in jobs] == [d3.delivery_id, d1.delivery_id, d2.delivery_id]


def test_reassigning_to_a_new_driver_puts_it_at_the_end_of_their_queue(db):
    """Dragging a card to a different driver shouldn't carry over its old
    sequence number and land it in an arbitrary spot in the new queue."""
    order1 = _confirmed_order(db, suffix="A", requested_date="2026-09-10")
    order2 = _confirmed_order(db, suffix="B", requested_date="2026-09-10")
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()

    # Sam already has one job today.
    sam_job = crud.create_delivery(db, order1.order_id, driver_user_id=sam.user_id, scheduled_date=date(2026, 9, 10))
    # Dan's job gets reassigned to Sam.
    dan_job = crud.create_delivery(db, order2.order_id, driver_user_id=dan.user_id, scheduled_date=date(2026, 9, 10))
    db.commit()

    crud.reassign_delivery(db, dan_job.delivery_id, driver_user_id=sam.user_id, vehicle_id=None)
    db.commit()

    jobs = crud.deliveries_for_driver(db, sam.user_id)
    assert [j.delivery_id for j in jobs] == [sam_job.delivery_id, dan_job.delivery_id]  # appended, not jumbled
