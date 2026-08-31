"""Same business-rule checks as the original desktop self_test.py, run against
the ported SQLAlchemy models/crud layer to prove the move to Postgres-ready
code didn't change behaviour."""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.models import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    crud.ensure_admin_user(session)
    session.commit()
    yield session
    session.close()


def test_login(db):
    assert crud.authenticate(db, "admin", "tigerone") is not None
    assert crud.authenticate(db, "admin", "wrong") is None


def test_quote_allocation_and_release(db):
    commercial = crud.save_customer(db, {
        "customer_type": "Commercial", "display_name": "Test Trade Ltd", "contact_name": "Buyer",
    })
    db.commit()

    material_ids = {}
    for code, name, unit, stock, reorder in (
        ("CEMENT", "Cement", "kg", 5000, 1000),
        ("SAND", "Sharp sand", "kg", 12000, 2000),
        ("AGG20", "20mm aggregate", "kg", 18000, 3000),
    ):
        material = crud.save_material(db, {
            "code": code, "name": name, "unit": unit, "on_hand": stock,
            "reorder_level": reorder, "reorder_quantity": reorder * 2,
            "unit_cost": 0.12, "supplier": "Test Supplier",
        })
        material_ids[code] = material.material_id
    db.commit()

    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "C30 ready-mix concrete",
        "sell_unit": "m³", "default_unit_price": 142.345,
    }, [
        {"material_id": material_ids["CEMENT"], "quantity_per_unit": 300, "waste_percent": 2},
        {"material_id": material_ids["SAND"], "quantity_per_unit": 700, "waste_percent": 2},
        {"material_id": material_ids["AGG20"], "quantity_per_unit": 1100, "waste_percent": 2},
    ])
    db.commit()

    quote = crud.save_quote(db, {
        "customer_id": commercial.customer_id, "project": "Factory slab", "site_address": "Test Site",
        "requested_date": "2026-09-04", "status": "Draft", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30 ready-mix concrete",
         "quantity": 10, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()

    payload = crud.quote_payload(db, quote.quote_id)
    assert payload["subtotal"] == Decimal("1423.50")
    assert payload["items"][0]["unit_price"] == Decimal("142.35")
    assert sum(row["reserved"] for row in crud.material_balances(db)) == 0

    crud.set_quote_status(db, quote.quote_id, "Accepted")
    db.commit()
    reserved = {row["code"]: row["reserved"] for row in crud.material_balances(db)}
    assert reserved["CEMENT"] == Decimal("3060.000")
    assert reserved["SAND"] == Decimal("7140.000")
    assert reserved["AGG20"] == Decimal("11220.000")

    crud.set_quote_status(db, quote.quote_id, "Lost")
    db.commit()
    assert sum(row["reserved"] for row in crud.material_balances(db)) == 0


def test_allocate_stock_failsafe(db):
    """An Accepted quote due next week can be told not to reserve stock yet,
    so materials stay free for jobs happening in the next day or two — then
    switched back on when it's actually needed."""
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    db.commit()

    quote = crud.save_quote(db, {
        "customer_id": commercial.customer_id, "project": "Next week's job", "site_address": "Test Site",
        "requested_date": "2026-09-08", "status": "Accepted", "tax_rate": 20,
        "allocate_stock": False,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 10, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()

    # Accepted, but allocate_stock=False — nothing should be reserved yet.
    assert quote.allocate_stock is False
    assert sum(row["reserved"] for row in crud.material_balances(db)) == 0

    # Office switches it on once the delivery is actually coming up.
    crud.set_quote_allocate_stock(db, quote.quote_id, True)
    db.commit()
    reserved = {row["code"]: row["reserved"] for row in crud.material_balances(db)}
    assert reserved["CEMENT"] == Decimal("3060.000")

    # And back off again without touching status or lines.
    crud.set_quote_allocate_stock(db, quote.quote_id, False)
    db.commit()
    assert sum(row["reserved"] for row in crud.material_balances(db)) == 0
    assert quote.status == "Accepted"


def test_accepted_quote_becomes_live_order(db):
    """Winning a quote should copy it straight into a live order, and the
    reservation should move with it — not get held twice."""
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    db.commit()

    quote = crud.save_quote(db, {
        "customer_id": commercial.customer_id, "project": "Won job", "site_address": "Test Site",
        "requested_date": "2026-09-08", "status": "Draft", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 10, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()
    assert quote.converted_orders == []

    crud.set_quote_status(db, quote.quote_id, "Accepted")
    db.commit()

    # A live order now exists, copied straight from the quote.
    assert len(quote.converted_orders) == 1
    order = quote.converted_orders[0]
    assert order.status == "Confirmed"
    assert order.customer_id == commercial.customer_id
    assert order.project == "Won job"
    assert order.total == quote.total
    assert len(order.items) == 1
    assert order.items[0].quantity == quote.items[0].quantity

    # Stock is reserved once via the order, not twice.
    assert quote.allocate_stock is False
    reserved = {row["code"]: row for row in crud.material_balances(db)}
    assert reserved["CEMENT"]["quote_reserved"] == Decimal("0")
    assert reserved["CEMENT"]["order_reserved"] == Decimal("3060.000")
    assert reserved["CEMENT"]["reserved"] == Decimal("3060.000")

    # Accepting it again (e.g. a duplicate click) must not create a second order.
    crud.set_quote_status(db, quote.quote_id, "Accepted")
    db.commit()
    assert len(quote.converted_orders) == 1

    # The manual allocate-stock toggle on the quote is now a no-op — the
    # order owns the reservation.
    crud.set_quote_allocate_stock(db, quote.quote_id, True)
    db.commit()
    assert quote.allocate_stock is False
    reserved = {row["code"]: row for row in crud.material_balances(db)}
    assert reserved["CEMENT"]["quote_reserved"] == Decimal("0")


def test_order_allocation(db):
    commercial = crud.save_customer(db, {
        "customer_type": "Commercial", "display_name": "Test Trade Ltd", "contact_name": "Buyer",
    })
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³",
        "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    db.commit()

    order = crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Direct yard order", "site_address": "Test Site",
        "requested_date": "2026-09-05", "status": "Draft", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30 ready-mix concrete",
         "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()

    assert crud.order_payload(db, order.order_id)["subtotal"] == Decimal("711.75")
    assert sum(row["order_reserved"] for row in crud.material_balances(db)) == 0

    crud.set_order_status(db, order.order_id, "Confirmed")
    db.commit()
    reserved = {row["code"]: row["order_reserved"] for row in crud.material_balances(db)}
    assert reserved["CEMENT"] == Decimal("1530.000")


def test_delivery_pod_and_tracking(db):
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    order = crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Test", "site_address": "Test Site",
        "requested_date": "2026-09-05", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, vehicle="TC01", driver_name="Dan")
    db.commit()
    assert crud.get_delivery_by_token(db, delivery.access_token) is not None

    crud.record_ping(db, delivery, 52.633, -1.238)
    db.commit()
    assert delivery.status == "En Route"
    assert len(delivery.pings) == 1

    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "photo.jpg", 52.634, -1.239)
    db.commit()
    assert delivery.status == "Delivered"
    assert delivery.pod_signed_by == "Site Foreman"


def test_driver_accounts_and_dashboard(db):
    """Driver logins (PIN-based) and the 'my jobs' query the driver dashboard uses."""
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    # authenticate() is the same function used for office logins — the PIN
    # is just what got hashed into password_hash for a Driver-role account.
    assert crud.authenticate(db, "dan", "4821") is not None
    assert crud.authenticate(db, "dan", "0000") is None
    assert driver.role == "Driver"

    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    order = crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Driveway", "site_address": "1 Test Rd",
        "requested_date": "2026-09-05", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")
    db.commit()

    # Assigning the driver by account derives driver_name automatically.
    delivery = crud.create_delivery(db, order.order_id, vehicle="TC01", driver_user_id=driver.user_id)
    db.commit()
    assert delivery.driver_name == "Dan Driver"
    assert delivery.scheduled_date is not None  # defaults to today when not given

    jobs = crud.deliveries_for_driver(db, driver.user_id)
    assert len(jobs) == 1
    assert jobs[0].delivery_id == delivery.delivery_id

    # Once delivered, it drops off the open-jobs list the dashboard shows.
    crud.record_pod(db, delivery, "Site Foreman", "sig.png", "", None, None)
    db.commit()
    assert crud.deliveries_for_driver(db, driver.user_id) == []
    assert len(crud.deliveries_for_driver(db, driver.user_id, include_delivered=True)) == 1
