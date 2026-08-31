"""Xero integration tests. We don't have real Xero credentials, so these
monkeypatch xero_client's HTTP calls with a small fake Xero server — this
still proves the actual logic (auto-push on customer create, auto-invoice
on Completed, idempotency, and that a Xero failure never blocks office
work), just not the real network round-trip."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import crud, xero_client


class FakeXero:
    """Minimal in-memory stand-in for Xero's Contacts/Invoices endpoints."""
    def __init__(self):
        self.contacts = {}  # ContactID -> contact dict
        self.invoices = {}  # InvoiceID -> invoice dict
        self.next_id = 1
        self.fail_invoices = False

    def _new_id(self):
        self.next_id += 1
        return f"id-{self.next_id}"


@pytest.fixture()
def fake_xero(monkeypatch):
    fake = FakeXero()

    def fake_find_or_create_contact(access_token, tenant_id, customer):
        for cid, c in fake.contacts.items():
            if c["Name"] == customer.display_name:
                return cid
        cid = fake._new_id()
        fake.contacts[cid] = {"ContactID": cid, "Name": customer.display_name,
                               "EmailAddress": customer.email}
        return cid

    def fake_create_invoice(access_token, tenant_id, order, xero_contact_id):
        if fake.fail_invoices:
            raise xero_client.XeroError("simulated Xero outage")
        iid = fake._new_id()
        fake.invoices[iid] = {"InvoiceID": iid, "InvoiceNumber": f"INV-{len(fake.invoices) + 1}",
                               "ContactID": xero_contact_id, "Reference": order.order_number}
        return iid, fake.invoices[iid]["InvoiceNumber"]

    def fake_refresh_tokens(client_id, client_secret, refresh_token):
        return {"access_token": "refreshed-token", "refresh_token": refresh_token, "expires_in": 1800}

    monkeypatch.setattr(xero_client, "find_or_create_contact", fake_find_or_create_contact)
    monkeypatch.setattr(xero_client, "create_invoice", fake_create_invoice)
    monkeypatch.setattr(xero_client, "refresh_tokens", fake_refresh_tokens)
    return fake


def _connect(db):
    return crud.save_xero_connection(db, "tenant-1", "Tiger Concrete Demo Co", {
        "access_token": "tok", "refresh_token": "refresh-tok",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }, "admin")


def test_customer_auto_pushed_to_xero_on_create(db, fake_xero):
    _connect(db)
    customer = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Acme Builders"})
    db.commit()
    assert customer.xero_contact_id is not None
    assert customer.xero_synced_at is not None
    assert fake_xero.contacts[customer.xero_contact_id]["Name"] == "Acme Builders"


def test_customer_save_still_works_without_xero_connected(db, fake_xero):
    # No _connect(db) call — Xero isn't connected at all.
    customer = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "No Xero Ltd"})
    db.commit()
    assert customer.customer_id is not None
    assert customer.xero_contact_id is None  # never even attempted


def test_completed_order_creates_xero_invoice(db, fake_xero):
    _connect(db)
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
    assert order.xero_invoice_id is None

    crud.set_order_status(db, order.order_id, "Completed")
    db.commit()

    assert order.xero_invoice_id is not None
    assert order.xero_invoice_number.startswith("INV-")
    invoice = fake_xero.invoices[order.xero_invoice_id]
    assert invoice["Reference"] == order.order_number


def test_completed_order_invoice_push_is_idempotent(db, fake_xero):
    _connect(db)
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

    crud.set_order_status(db, order.order_id, "Completed")
    db.commit()
    first_invoice_id = order.xero_invoice_id
    invoice_count_after_first = len(fake_xero.invoices)

    # Re-saving Completed (e.g. someone clicks it again) must not create a second invoice.
    crud.set_order_status(db, order.order_id, "Completed")
    db.commit()
    assert order.xero_invoice_id == first_invoice_id
    assert len(fake_xero.invoices) == invoice_count_after_first


def test_order_completion_survives_xero_outage(db, fake_xero):
    """A Xero failure must never stop the order itself from being marked Completed."""
    _connect(db)
    fake_xero.fail_invoices = True
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

    order = crud.set_order_status(db, order.order_id, "Completed")
    db.commit()
    assert order.status == "Completed"  # the office-facing action succeeded
    assert order.xero_invoice_id is None  # but nothing was pushed


def test_token_refresh_happens_when_expired(db, fake_xero):
    connection = crud.save_xero_connection(db, "tenant-1", "Demo Co", {
        "access_token": "stale-token", "refresh_token": "refresh-tok",
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),  # already expired
    }, "admin")
    db.commit()

    refreshed = crud.ensure_valid_xero_token(db, "client-id", "client-secret")
    assert refreshed.access_token == "refreshed-token"


def test_disconnect_removes_connection(db, fake_xero):
    _connect(db)
    assert crud.get_xero_connection(db) is not None
    crud.disconnect_xero(db)
    db.commit()
    assert crud.get_xero_connection(db) is None
