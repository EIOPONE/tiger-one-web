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
