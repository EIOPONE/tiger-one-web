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
