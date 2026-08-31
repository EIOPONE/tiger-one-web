"""Tests for the vehicle fleet, driver deactivation, delivery reassignment,
and daily vehicle checks."""
from datetime import datetime, timezone

from app import crud


def test_vehicle_crud(db):
    vehicle = crud.save_vehicle(db, "tc01", "8-wheel mixer")
    db.commit()
    assert vehicle.registration == "TC01"  # normalised to uppercase

    vehicles = crud.list_vehicles(db)
    assert len(vehicles) == 1

    crud.deactivate_vehicle(db, vehicle.vehicle_id)
    db.commit()
    assert crud.list_vehicles(db) == []


def test_driver_deactivate_removes_from_active_list_but_keeps_history(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    assert len(crud.list_drivers(db)) == 1

    crud.deactivate_driver(db, driver.user_id)
    db.commit()
    assert crud.list_drivers(db) == []
    # login should now fail for a deactivated driver
    assert crud.authenticate(db, "dan", "4821") is None
    # but the account record itself still exists (for delivery history)
    assert db.get(crud.models.AppUser, driver.user_id) is not None


def _confirmed_order(db):
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    return crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Driveway", "site_address": "1 Test Rd",
        "requested_date": "2026-09-05", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")


def test_reassign_delivery_changes_driver_and_vehicle(db):
    order = _confirmed_order(db)
    driver1 = crud.create_driver(db, "Dan Driver", "dan", "1111")
    driver2 = crud.create_driver(db, "Sam Driver", "sam", "2222")
    vehicle1 = crud.save_vehicle(db, "TC01")
    vehicle2 = crud.save_vehicle(db, "TC02")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver1.user_id, vehicle_id=vehicle1.vehicle_id)
    db.commit()
    assert delivery.driver_name == "Dan Driver"
    assert delivery.vehicle == "TC01"

    crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=driver2.user_id, vehicle_id=vehicle2.vehicle_id)
    db.commit()
    assert delivery.driver_name == "Sam Driver"
    assert delivery.vehicle == "TC02"
    assert delivery.driver_user_id == driver2.user_id

    # the reassigned driver's dashboard now shows it; the old driver's doesn't
    assert len(crud.deliveries_for_driver(db, driver2.user_id)) == 1
    assert len(crud.deliveries_for_driver(db, driver1.user_id)) == 0


def test_cannot_reassign_a_delivered_run(db):
    order = _confirmed_order(db)
    driver1 = crud.create_driver(db, "Dan Driver", "dan", "1111")
    driver2 = crud.create_driver(db, "Sam Driver", "sam", "2222")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver1.user_id)
    db.commit()
    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "", None, None)
    db.commit()
    assert delivery.status == "Delivered"

    try:
        crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=driver2.user_id, vehicle_id=None)
        assert False, "expected a ValueError"
    except ValueError:
        pass
    assert delivery.driver_user_id == driver1.user_id  # unchanged


def test_vehicle_check_submission_and_defect_flag(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    assert crud.driver_has_checked_in_today(db, driver.user_id) is False

    items = {key: "pass" for group in crud.WALKAROUND_CHECKLIST for key, _ in group["checks"]}
    items["tyres_wheels"] = "defect"  # one item flagged
    crud.save_vehicle_check(db, driver.user_id, vehicle.vehicle_id, items,
                             defect_notes="Nearside rear tyre worn", signed_by="Dan Driver", signature_path="sig.png")
    db.commit()

    assert crud.driver_has_checked_in_today(db, driver.user_id) is True
    checks = crud.list_vehicle_checks(db)
    assert len(checks) == 1
    assert checks[0].has_defects is True
    assert checks[0].defect_notes == "Nearside rear tyre worn"


def test_vehicle_check_all_pass_has_no_defect_flag(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.commit()

    items = {key: "pass" for group in crud.WALKAROUND_CHECKLIST for key, _ in group["checks"]}
    crud.save_vehicle_check(db, driver.user_id, vehicle.vehicle_id, items,
                             defect_notes="", signed_by="Dan Driver", signature_path="sig.png")
    db.commit()

    checks = crud.list_vehicle_checks(db)
    assert checks[0].has_defects is False


def test_deliveries_completed_since_notification_feed(db):
    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    marker = datetime.now(timezone.utc)

    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    assert crud.deliveries_completed_since(db, marker) == []

    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "", None, None)
    db.commit()

    recent = crud.deliveries_completed_since(db, marker)
    assert len(recent) == 1
    assert recent[0].delivery_id == delivery.delivery_id
