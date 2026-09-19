"""Covers the "snag list" fixes: edit-in-place for input records, the admin
-managed Payment Terms / Customer Group option lists, and CSV customer
import."""
from app import crud


# --- edit-in-place ------------------------------------------------------------

def test_edit_customer_in_place(db):
    customer = crud.save_customer(db, {
        "customer_type": "Commercial", "display_name": "Original Ltd",
        "payment_terms": "30 Days", "customer_group": "",
    })
    db.commit()

    edited = crud.save_customer(db, {
        "customer_type": "Commercial", "display_name": "Renamed Ltd",
        "payment_terms": "Cash", "customer_group": "Discounted Rate",
    }, customer_id=customer.customer_id)
    db.commit()

    assert edited.customer_id == customer.customer_id  # same row, not a duplicate
    assert edited.display_name == "Renamed Ltd"
    assert edited.payment_terms == "Cash"
    assert edited.customer_group == "Discounted Rate"


def test_edit_material_in_place(db):
    material = crud.save_material(db, {
        "code": "CEM", "name": "Cement", "unit": "kg", "on_hand": 100,
        "reorder_level": 10, "reorder_quantity": 50, "unit_cost": 0.5, "supplier": "ABC",
    })
    db.commit()

    edited = crud.save_material(db, {
        "code": "CEM", "name": "Cement (renamed)", "unit": "kg",
        "reorder_level": 20, "reorder_quantity": 60, "unit_cost": 0.6, "supplier": "XYZ",
    }, material_id=material.material_id)
    db.commit()

    assert edited.material_id == material.material_id
    assert edited.name == "Cement (renamed)"
    assert edited.supplier == "XYZ"
    assert float(edited.on_hand) == 100  # editing shouldn't touch stock — that's "receive stock"'s job


def test_edit_product_replaces_recipe(db):
    material = crud.save_material(db, {
        "code": "AGG", "name": "Aggregate", "unit": "kg", "on_hand": 1000,
        "reorder_level": 100, "reorder_quantity": 200, "unit_cost": 0.1, "supplier": "ABC",
    })
    db.commit()

    product = crud.save_product(db, {
        "code": "C30", "name": "C30", "description": "", "sell_unit": "m³", "default_unit_price": 90,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    db.commit()
    assert len(product.recipes) == 1

    edited = crud.save_product(db, {
        "code": "C30", "name": "C30 (renamed)", "description": "", "sell_unit": "m³", "default_unit_price": 99,
    }, [{"material_id": material.material_id, "quantity_per_unit": 320, "waste_percent": 3}],
        product_id=product.product_id)
    db.commit()

    assert edited.product_id == product.product_id
    assert edited.name == "C30 (renamed)"
    assert len(edited.recipes) == 1
    assert float(edited.recipes[0].quantity_per_unit) == 320


def test_update_driver_keeps_pin_when_blank(db):
    driver = crud.create_driver(db, "Dave Driver", "dave", "1234")
    db.commit()
    original_hash = driver.password_hash

    crud.update_driver(db, driver.user_id, "Dave Renamed", "dave", pin="")
    db.commit()
    assert driver.full_name == "Dave Renamed"
    assert driver.password_hash == original_hash  # blank pin = keep existing

    crud.update_driver(db, driver.user_id, "Dave Renamed", "dave", pin="9999")
    db.commit()
    assert driver.password_hash != original_hash
    assert crud.authenticate(db, "dave", "9999") is not None


def test_update_office_user_keeps_password_when_blank(db):
    staff = crud.create_office_user(db, "Sarah Office", "sarah", "pass123", "Office")
    db.commit()
    original_hash = staff.password_hash

    crud.update_office_user(db, staff.user_id, "Sarah Renamed", "sarah", "Admin", password="")
    db.commit()
    assert staff.full_name == "Sarah Renamed"
    assert staff.role == "Admin"
    assert staff.password_hash == original_hash

    crud.update_office_user(db, staff.user_id, "Sarah Renamed", "sarah", "Admin", password="newpass")
    db.commit()
    assert staff.password_hash != original_hash
    assert crud.authenticate(db, "sarah", "newpass") is not None


def test_deactivate_material_removes_it_from_active_list_but_keeps_the_row(db):
    material = crud.save_material(db, {
        "code": "CEM", "name": "Cement", "unit": "kg", "on_hand": 100,
        "reorder_level": 10, "reorder_quantity": 50, "unit_cost": 0.5, "supplier": "ABC",
    })
    db.commit()
    assert any(m["material_id"] == material.material_id for m in crud.material_balances(db))

    crud.deactivate_material(db, material.material_id)
    db.commit()
    assert not any(m["material_id"] == material.material_id for m in crud.material_balances(db))
    assert db.get(crud.models.Material, material.material_id) is not None  # row kept, just inactive


def test_material_delete_impact_counts_active_product_recipes(db):
    material = crud.save_material(db, {
        "code": "AGG", "name": "Aggregate", "unit": "kg", "on_hand": 1000,
        "reorder_level": 100, "reorder_quantity": 200, "unit_cost": 0.1, "supplier": "ABC",
    })
    db.commit()
    assert crud.material_delete_impact(db, material.material_id)["recipe_count"] == 0

    product = crud.save_product(db, {
        "code": "C30", "name": "C30", "description": "", "sell_unit": "m³", "default_unit_price": 90,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    db.commit()
    assert crud.material_delete_impact(db, material.material_id)["recipe_count"] == 1

    crud.deactivate_product(db, product.product_id)
    db.commit()
    assert crud.material_delete_impact(db, material.material_id)["recipe_count"] == 0  # product no longer active


def test_deactivate_product_removes_it_from_active_list_but_keeps_the_row(db):
    product = crud.save_product(db, {
        "code": "C30", "name": "C30", "description": "", "sell_unit": "m³", "default_unit_price": 90,
    }, [])
    db.commit()

    crud.deactivate_product(db, product.product_id)
    db.commit()
    assert product.active is False
    assert db.get(crud.models.Product, product.product_id) is not None


def test_edit_vehicle_in_place(db):
    vehicle = crud.save_vehicle(db, "TC01", "Mixer")
    db.commit()

    edited = crud.save_vehicle(db, "TC01X", "Mixer (renamed)", vehicle_id=vehicle.vehicle_id)
    db.commit()
    assert edited.vehicle_id == vehicle.vehicle_id
    assert edited.registration == "TC01X"
    assert edited.description == "Mixer (renamed)"


# --- admin-managed dropdown option lists ---------------------------------------

def test_payment_terms_options_add_and_remove(db):
    assert crud.payment_terms_options(db) == []

    crud.add_payment_terms_option(db, "Pro Forma")
    crud.add_payment_terms_option(db, "30 Days")
    db.commit()
    names = [o.name for o in crud.payment_terms_options(db)]
    assert names == ["Pro Forma", "30 Days"]

    option = crud.payment_terms_options(db)[0]
    crud.remove_payment_terms_option(db, option.option_id)
    db.commit()
    names = [o.name for o in crud.payment_terms_options(db)]
    assert names == ["30 Days"]  # removed one no longer offered...
    all_options = crud.payment_terms_options(db, active_only=False)
    assert len(all_options) == 2  # ...but the row (and any customer text using it) isn't deleted


def test_customer_group_options_add_and_remove(db):
    crud.add_customer_group_option(db, "Late Payer")
    db.commit()
    assert [o.name for o in crud.customer_group_options(db)] == ["Late Payer"]

    option = crud.customer_group_options(db)[0]
    crud.remove_customer_group_option(db, option.option_id)
    db.commit()
    assert crud.customer_group_options(db) == []


def test_removing_and_readding_an_option_reactivates_it(db):
    """Re-adding a name that was previously removed reactivates the same
    row rather than creating a duplicate — keeps the options list clean."""
    opt = crud.add_payment_terms_option(db, "Cash")
    db.commit()
    crud.remove_payment_terms_option(db, opt.option_id)
    db.commit()
    assert crud.payment_terms_options(db) == []

    crud.add_payment_terms_option(db, "Cash")
    db.commit()
    active = crud.payment_terms_options(db)
    assert len(active) == 1
    assert active[0].option_id == opt.option_id  # same row, reactivated


# --- CSV customer import -------------------------------------------------------

def test_import_customers_csv_happy_path(db):
    csv_text = (
        "Name,Type,Contact Name,Mobile,Email,Payment Terms,Customer Group,Town\n"
        "Acme Builders,Commercial,Jane Smith,07700900123,jane@acme.example,End Of Month,Discounted Rate,Leicester\n"
        "Bob Jones,Private,,,,,,\n"
    )
    result = crud.import_customers_csv(db, csv_text)
    db.commit()

    assert result["created"] == 2
    assert result["skipped"] == 0

    acme = db.query(crud.models.Customer).filter_by(display_name="Acme Builders").one()
    assert acme.customer_type == "Commercial"
    assert acme.contact_name == "Jane Smith"
    assert acme.payment_terms == "End Of Month"
    assert acme.customer_group == "Discounted Rate"
    assert acme.town == "Leicester"

    bob = db.query(crud.models.Customer).filter_by(display_name="Bob Jones").one()
    assert bob.customer_type == "Private"


def test_import_customers_csv_skips_rows_without_a_name(db):
    csv_text = "Name,Mobile\n,07700900000\nGood Customer,07700900111\n"
    result = crud.import_customers_csv(db, csv_text)
    db.commit()

    assert result["created"] == 1
    assert result["skipped"] == 1
    assert "Row 2" in result["errors"][0]


def test_import_customers_csv_defaults_bad_customer_type_to_commercial(db):
    csv_text = "Name,Type\nWeird Co,Wholesale\n"
    result = crud.import_customers_csv(db, csv_text)
    db.commit()
    assert result["created"] == 1
    customer = db.query(crud.models.Customer).filter_by(display_name="Weird Co").one()
    assert customer.customer_type == "Commercial"


def test_import_customers_csv_recognises_alternate_headers(db):
    """Different exports spell the name column differently — 'Company',
    'Customer Name' etc should all work, not just the literal word 'Name'."""
    csv_text = "Company,Terms\nAlt Header Co,Cash\n"
    result = crud.import_customers_csv(db, csv_text)
    db.commit()
    assert result["created"] == 1
    customer = db.query(crud.models.Customer).filter_by(display_name="Alt Header Co").one()
    assert customer.payment_terms == "Cash"
