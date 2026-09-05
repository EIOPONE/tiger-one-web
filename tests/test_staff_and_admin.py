"""Office staff logins, the Admin role, and hard-deleting quotes/orders."""
from decimal import Decimal

from app import crud


def test_seeded_admin_account_has_admin_role(db):
    """ensure_admin_user's seeded account is the account this whole
    permission model hangs off — must be role 'Admin', not the old
    'Commercial Manager'."""
    admin = crud.authenticate(db, "admin", "tigerone")
    assert admin is not None
    assert admin.role == "Admin"


def test_ensure_admin_user_promotes_existing_account_on_upgrade(db):
    """Simulates a live database from before this change — an existing
    'admin' account with the old role must get promoted, not left behind."""
    existing = crud.authenticate(db, "admin", "tigerone")
    existing.role = "Commercial Manager"  # simulate the pre-upgrade state
    db.commit()

    crud.ensure_admin_user(db)  # called again, as it is on every app startup
    db.commit()

    refreshed = crud.authenticate(db, "admin", "tigerone")
    assert refreshed.role == "Admin"


def test_create_and_list_office_users(db):
    crud.create_office_user(db, "Sarah Jones", "sarah", "hunter2", "Sales")
    db.commit()

    staff = crud.list_office_users(db)
    usernames = {s.username for s in staff}
    assert "sarah" in usernames
    assert "admin" in usernames  # the seeded account is office staff too

    sarah = crud.authenticate(db, "sarah", "hunter2")
    assert sarah is not None
    assert sarah.role == "Sales"


def test_office_users_list_excludes_drivers(db):
    crud.create_office_user(db, "Sarah Jones", "sarah", "hunter2", "Sales")
    crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    staff_usernames = {s.username for s in crud.list_office_users(db)}
    assert "dan" not in staff_usernames


def test_deactivate_office_user_blocks_login_but_keeps_record(db):
    user = crud.create_office_user(db, "Sarah Jones", "sarah", "hunter2", "Sales")
    db.commit()

    crud.deactivate_office_user(db, user.user_id)
    db.commit()

    assert crud.authenticate(db, "sarah", "hunter2") is None
    assert db.get(crud.models.AppUser, user.user_id) is not None


def _confirmed_order_and_quote(db):
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    quote = crud.save_quote(db, {
        "customer_id": commercial.customer_id, "project": "Test job", "site_address": "1 Test Rd",
        "requested_date": "2026-09-05", "status": "Accepted", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 10, "unit": "m³", "unit_price": 142.345}], "admin")
    order = crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Test job", "site_address": "1 Test Rd",
        "requested_date": "2026-09-05", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")
    return quote, order, material


def test_delete_quote_releases_its_reservation(db):
    quote, _order, material = _confirmed_order_and_quote(db)
    db.commit()
    assert sum(row["quote_reserved"] for row in crud.material_balances(db)) > 0

    crud.delete_quote(db, quote.quote_id)
    db.commit()

    assert db.get(crud.models.Quote, quote.quote_id) is None
    assert sum(row["quote_reserved"] for row in crud.material_balances(db)) == 0


def test_delete_order_releases_its_reservation_and_cascades_deliveries(db):
    _quote, order, material = _confirmed_order_and_quote(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    delivery_id = delivery.delivery_id

    crud.delete_order(db, order.order_id)
    db.commit()

    assert db.get(crud.models.Order, order.order_id) is None
    assert db.get(crud.models.Delivery, delivery_id) is None  # cascaded, not orphaned
    assert sum(row["order_reserved"] for row in crud.material_balances(db)) == 0


def test_delete_is_harmless_on_a_nonexistent_id(db):
    """Admin double-clicking delete, or a stale page — must not raise."""
    crud.delete_quote(db, 99999)
    crud.delete_order(db, 99999)
