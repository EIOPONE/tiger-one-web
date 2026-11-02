"""Customers page is searchable and paged instead of loading everyone."""
from app import crud


def _make(db, n):
    for i in range(n):
        crud.save_customer(db, {
            "customer_type": "Commercial", "display_name": f"Customer {i:03d}",
            "town": "Leicester" if i % 2 else "Derby", "email": f"c{i}@example.com",
        }, sync_xero=False)
    db.commit()


def test_pages_are_25_and_ordered(db):
    _make(db, 60)
    rows, total = crud.search_customers(db, "", 1)
    assert total == 60 and len(rows) == 25 and rows[0].display_name == "Customer 000"
    rows3, _ = crud.search_customers(db, "", 3)
    assert len(rows3) == 10 and rows3[0].display_name == "Customer 050"


def test_search_matches_name_town_and_email_case_insensitively(db):
    _make(db, 10)
    assert crud.search_customers(db, "customer 007")[1] == 1
    assert crud.search_customers(db, "LEICESTER")[1] == 5
    assert crud.search_customers(db, "c3@example")[1] == 1
    assert crud.search_customers(db, "nobody")[1] == 0


def test_search_treats_percent_and_underscore_literally(db):
    crud.save_customer(db, {"customer_type": "Private", "display_name": "100% Builders"}, sync_xero=False)
    crud.save_customer(db, {"customer_type": "Private", "display_name": "Other Co"}, sync_xero=False)
    db.commit()
    rows, total = crud.search_customers(db, "%")
    assert total == 1 and rows[0].display_name == "100% Builders"


def test_delete_impacts_batched(db):
    _make(db, 3)
    ids = [c.customer_id for c in crud.search_customers(db, "")[0]]
    impacts = crud.customer_delete_impacts(db, ids)
    assert set(impacts) == set(ids) and impacts[ids[0]] == {"quote_count": 0, "order_count": 0}


def _order(db, customer, number_suffix, project, status="Confirmed"):
    from tests.test_kanban import _confirmed_order  # noqa: F401  (ensures product/material helpers import)
    material = crud.save_material(db, {"code": f"M{number_suffix}", "name": "Cement", "unit": "kg", "on_hand": 5000,
                                       "reorder_level": 1, "reorder_quantity": 1, "unit_cost": 0.1, "supplier": ""})
    product = crud.save_product(db, {"code": f"P{number_suffix}", "name": "C30", "description": "", "sell_unit": "m³",
                                     "default_unit_price": 100},
                                [{"material_id": material.material_id, "quantity_per_unit": 1, "waste_percent": 0}])
    return crud.save_order(db, {"customer_id": customer.customer_id, "project": project, "site_address": "1 High St",
                                "status": status, "tax_rate": 20},
                           [{"product_id": product.product_id, "description": "C30", "quantity": 1, "unit": "m³", "unit_price": 100}], "admin")


def test_order_search_by_customer_project_and_status(db):
    a = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Alpha Builders"}, sync_xero=False)
    b = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Beta Homes"}, sync_xero=False)
    _order(db, a, "1", "Garage slab", "Draft")
    _order(db, b, "2", "Driveway", "Confirmed")
    db.commit()
    assert crud.search_orders(db, "alpha")[1] == 1
    assert crud.search_orders(db, "driveway")[1] == 1
    assert crud.search_orders(db, "", "Draft")[1] == 1
    assert crud.search_orders(db, "alpha", "Confirmed")[1] == 0
    assert crud.search_orders(db)[1] == 2
    assert crud.search_quotes(db)[1] == 0
