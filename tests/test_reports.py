"""Sales report tests — date-range filtering on Completed orders and the totals shown."""
from decimal import Decimal

from app import crud


def _make_order(db, requested_date, status, quantity=5):
    commercial = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": f"MAT-{requested_date}", "name": "Cement", "unit": "kg", "on_hand": 50000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": f"P-{requested_date}", "name": "C30 Concrete", "description": "", "sell_unit": "m³",
        "default_unit_price": 100,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 0}])
    return crud.save_order(db, {
        "customer_id": commercial.customer_id, "project": "Test job", "site_address": "1 Test Rd",
        "requested_date": requested_date, "status": status, "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": quantity, "unit": "m³", "unit_price": 100}], "admin")


def test_report_only_includes_completed_orders_in_range(db):
    _make_order(db, "2026-08-25", "Completed", quantity=5)   # in range, counts
    _make_order(db, "2026-08-30", "Confirmed", quantity=5)   # in range but not delivered — excluded
    _make_order(db, "2026-09-10", "Completed", quantity=5)   # outside range — excluded
    db.commit()

    report = crud.sales_report(db, "2026-08-24", "2026-08-31")
    assert report["order_count"] == 1
    assert report["subtotal"] == Decimal("500.00")
    assert report["total"] == Decimal("600.00")  # +20% tax
    assert report["orders"][0].requested_date == "2026-08-25"


def test_report_totals_sum_multiple_orders(db):
    _make_order(db, "2026-08-25", "Completed", quantity=5)
    _make_order(db, "2026-08-26", "Completed", quantity=3)
    db.commit()

    report = crud.sales_report(db, "2026-08-24", "2026-08-31")
    assert report["order_count"] == 2
    assert report["subtotal"] == Decimal("800.00")
    assert report["total"] == Decimal("960.00")


def test_report_empty_range_returns_zeroed_totals(db):
    report = crud.sales_report(db, "2026-01-01", "2026-01-07")
    assert report["order_count"] == 0
    assert report["subtotal"] == Decimal("0")
    assert report["orders"] == []
