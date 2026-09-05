"""Business rules — recipes, allocation, quote/order lifecycle.

This mirrors the original Tiger One desktop database.py function-for-function
where possible, so the same behaviour (and the same self-tests, ported into
tests/test_business_rules.py) carry over unchanged.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from . import xero_client
from . import traccar_client
from .security import hash_password, verify_password, new_access_token

XERO_CLIENT_ID = os.environ.get("XERO_CLIENT_ID", "")
XERO_CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET", "")

TWOPLACES = Decimal("0.01")
QUOTE_STATUSES = ("Draft", "Issued", "Accepted", "Lost", "Cancelled")
ORDER_STATUSES = ("Draft", "Confirmed", "Completed", "Cancelled")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# --- users -------------------------------------------------------------------

def ensure_admin_user(db: Session) -> None:
    existing = db.scalar(select(models.AppUser).where(models.AppUser.username == "admin"))
    if existing:
        if existing.role != "Admin":
            existing.role = "Admin"  # promotes the existing seeded account on upgrade, not just new installs
            db.flush()
        return
    db.add(models.AppUser(
        username="admin", password_hash=hash_password("tigerone"),
        full_name="Daniel Anderson", role="Admin",
    ))
    db.flush()


def create_office_user(db: Session, full_name: str, username: str, password: str, role: str) -> models.AppUser:
    user = models.AppUser(
        username=username.strip().lower(), password_hash=hash_password(password),
        full_name=full_name.strip(), role=role.strip() or "Office",
    )
    db.add(user)
    db.flush()
    return user


def list_office_users(db: Session) -> list[models.AppUser]:
    return list(db.scalars(
        select(models.AppUser)
        .where(models.AppUser.role != "Driver", models.AppUser.active.is_(True))
        .order_by(models.AppUser.full_name)
    ))


def deactivate_office_user(db: Session, user_id: int) -> None:
    user = db.get(models.AppUser, user_id)
    if user:
        user.active = False
        db.flush()


def create_driver(db: Session, full_name: str, username: str, pin: str) -> models.AppUser:
    """A driver account authenticates with a short PIN instead of a password —
    same hashing underneath (crud.authenticate doesn't care which it was)."""
    driver = models.AppUser(
        username=username.strip().lower(), password_hash=hash_password(pin),
        full_name=full_name.strip(), role="Driver",
    )
    db.add(driver)
    db.flush()
    return driver


def list_drivers(db: Session) -> list[models.AppUser]:
    return list(db.scalars(
        select(models.AppUser)
        .where(models.AppUser.role == "Driver", models.AppUser.active.is_(True))
        .order_by(models.AppUser.full_name)
    ))


def deactivate_driver(db: Session, driver_user_id: int) -> None:
    """Soft-delete — keeps the account (and its history of deliveries/checks
    intact) but takes it off the active driver list and blocks login."""
    driver = db.get(models.AppUser, driver_user_id)
    if driver:
        driver.active = False
        db.flush()


def authenticate(db: Session, username: str, password: str) -> models.AppUser | None:
    user = db.scalar(
        select(models.AppUser).where(
            func.lower(models.AppUser.username) == username.strip().lower(),
            models.AppUser.active.is_(True),
        )
    )
    if user and verify_password(password, user.password_hash):
        return user
    return None


# --- numbering -----------------------------------------------------------------

def next_quote_number(db: Session) -> str:
    count = db.scalar(select(func.count(models.Quote.quote_id))) or 0
    return f"TCQ {count + 1:05d}"


def next_order_number(db: Session) -> str:
    count = db.scalar(select(func.count(models.Order.order_id))) or 0
    return f"TCO {count + 1:05d}"


# --- customers / materials / products -------------------------------------------

def save_customer(db: Session, values: dict, customer_id: int | None = None) -> models.Customer:
    is_new = customer_id is None
    if customer_id:
        customer = db.get(models.Customer, customer_id)
        for key, value in values.items():
            setattr(customer, key, value)
    else:
        customer = models.Customer(**values)
        db.add(customer)
    db.flush()
    if is_new:
        sync_customer_to_xero(db, customer, XERO_CLIENT_ID, XERO_CLIENT_SECRET)
    return customer


def save_material(db: Session, values: dict, material_id: int | None = None) -> models.Material:
    if material_id:
        material = db.get(models.Material, material_id)
        for key, value in values.items():
            setattr(material, key, value)
    else:
        material = models.Material(**values)
        db.add(material)
    db.flush()
    return material


def receive_stock(db: Session, material_id: int, quantity: float, reference: str = "", notes: str = "") -> None:
    material = db.get(models.Material, material_id)
    if not material:
        raise ValueError("Material not found")
    material.on_hand = Decimal(str(material.on_hand)) + Decimal(str(quantity))
    db.flush()


def save_product(db: Session, values: dict, recipe_lines: list[dict], product_id: int | None = None) -> models.Product:
    if product_id:
        product = db.get(models.Product, product_id)
        for key, value in values.items():
            setattr(product, key, value)
        product.recipes.clear()
        db.flush()
    else:
        product = models.Product(**values)
        db.add(product)
        db.flush()
    for line in recipe_lines:
        db.add(models.Recipe(
            product_id=product.product_id,
            material_id=int(line["material_id"]),
            quantity_per_unit=Decimal(str(line["quantity_per_unit"])),
            waste_percent=Decimal(str(line.get("waste_percent", 0) or 0)),
        ))
    db.flush()
    return product


def recipe_for_product(db: Session, product_id: int) -> list[models.Recipe]:
    return list(db.scalars(select(models.Recipe).where(models.Recipe.product_id == product_id)))


# --- quotes ----------------------------------------------------------------------

def _prepare_items(items: list[dict]) -> tuple[list[dict], Decimal]:
    if not items:
        raise ValueError("A quotation must contain at least one line")
    prepared = []
    subtotal = Decimal("0")
    for index, item in enumerate(items, start=1):
        quantity = Decimal(str(item.get("quantity") or 0))
        unit_price = money(item.get("unit_price") or 0)
        if quantity <= 0 or unit_price < 0:
            raise ValueError("Quantities must be positive and prices cannot be negative")
        line_total = money(quantity * unit_price)
        subtotal += line_total
        prepared.append({
            "line_number": index,
            "product_id": int(item["product_id"]) if item.get("product_id") else None,
            "description": str(item.get("description", "")).strip(),
            "quantity": quantity,
            "unit": str(item.get("unit", "m³")).strip(),
            "unit_price": unit_price,
            "line_total": line_total,
        })
    return prepared, money(subtotal)


def save_quote(db: Session, header: dict, items: list[dict], username: str, quote_id: int | None = None) -> models.Quote:
    prepared, subtotal = _prepare_items(items)
    tax_rate = Decimal(str(header.get("tax_rate", 20) or 0))
    tax_total = money(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax_total
    status = str(header.get("status", "Draft"))
    if status not in QUOTE_STATUSES:
        raise ValueError("Invalid quotation status")

    if quote_id:
        quote = db.get(models.Quote, quote_id)
        quote.items.clear()
    else:
        quote = models.Quote(
            quote_number=str(header.get("quote_number") or next_quote_number(db)),
            created_by=username,
        )
        db.add(quote)

    quote.revision = str(header.get("revision", "A")).strip() or "A"
    quote.customer_id = int(header["customer_id"])
    quote.project = str(header.get("project", "")).strip()
    quote.site_address = str(header.get("site_address", "")).strip()
    quote.requested_date = str(header.get("requested_date", "")).strip()
    quote.status = status
    quote.validity_days = int(header.get("validity_days", 14) or 14)
    quote.commercial_notes = str(header.get("commercial_notes", "")).strip()
    quote.subtotal = subtotal
    quote.tax_rate = tax_rate
    quote.tax_total = tax_total
    quote.total = total
    if "allocate_stock" in header:
        quote.allocate_stock = bool(header["allocate_stock"])
    elif quote_id is None:
        quote.allocate_stock = True
    db.flush()

    for row in prepared:
        db.add(models.QuoteItem(quote_id=quote.quote_id, **row))
    db.flush()

    _rebuild_quote_reservations(db, quote)
    return quote


def _rebuild_quote_reservations(db: Session, quote: models.Quote) -> None:
    quote.reservations.clear()
    db.flush()
    if quote.status != "Accepted" or not quote.allocate_stock:
        return
    requirements = db.execute(
        select(
            models.Recipe.material_id,
            func.sum(
                models.QuoteItem.quantity * models.Recipe.quantity_per_unit
                * (1 + models.Recipe.waste_percent / Decimal("100"))
            ),
        )
        .join(models.QuoteItem, models.QuoteItem.product_id == models.Recipe.product_id)
        .where(models.QuoteItem.quote_id == quote.quote_id)
        .group_by(models.Recipe.material_id)
    ).all()
    for material_id, required in requirements:
        db.add(models.MaterialReservation(
            quote_id=quote.quote_id, material_id=material_id, quantity=required,
        ))
    db.flush()


def set_quote_status(db: Session, quote_id: int, status: str) -> models.Quote:
    if status not in QUOTE_STATUSES:
        raise ValueError("Invalid quotation status")
    quote = db.get(models.Quote, quote_id)
    quote.status = status
    db.flush()

    if status == "Accepted" and not quote.converted_orders:
        # Won — copy it straight into a live order so it shows up on the
        # jobs board, instead of living on as a quote forever.
        _convert_quote_to_order(db, quote)
        # The order now owns the stock reservation for this job; the quote
        # itself stops reserving so the same material isn't held twice.
        quote.allocate_stock = False
    elif status in ("Lost", "Cancelled"):
        # A quote that was accepted (and so already converted) can still
        # fall through afterwards — cancel the order it became too, so
        # the stock it was holding is released rather than reserved forever.
        for order in quote.converted_orders:
            if order.status not in ("Completed", "Cancelled"):
                set_order_status(db, order.order_id, "Cancelled")

    _rebuild_quote_reservations(db, quote)
    return quote


def _convert_quote_to_order(db: Session, quote: models.Quote) -> models.Order:
    order = models.Order(
        order_number=next_order_number(db),
        created_by=quote.created_by,
        source_quote_id=quote.quote_id,
        customer_id=quote.customer_id,
        project=quote.project,
        site_address=quote.site_address,
        requested_date=quote.requested_date,
        status="Confirmed",
        commercial_notes=quote.commercial_notes,
        subtotal=quote.subtotal,
        tax_rate=quote.tax_rate,
        tax_total=quote.tax_total,
        total=quote.total,
        # Carries over whatever the quote's allocate-stock failsafe was set
        # to, so a quote that was deliberately left un-reserved (e.g. job's
        # a week out) doesn't suddenly grab stock the moment it's won.
        allocate_stock=quote.allocate_stock,
    )
    db.add(order)
    db.flush()
    for line_number, item in enumerate(sorted(quote.items, key=lambda i: i.line_number), start=1):
        db.add(models.OrderItem(
            order_id=order.order_id, line_number=line_number, product_id=item.product_id,
            description=item.description, quantity=item.quantity, unit=item.unit,
            unit_price=item.unit_price, line_total=item.line_total,
        ))
    db.flush()
    _rebuild_order_reservations(db, order)
    return order


def set_quote_allocate_stock(db: Session, quote_id: int, allocate: bool) -> models.Quote:
    """The manual 'allocate stock: yes/no' failsafe. Lets the office un-reserve
    materials for an Accepted quote that isn't needed for a while, so stock
    stays free for jobs happening in the next day or two — without changing
    the quote's status or touching its lines.

    Once a quote has been won and converted to an order, the order owns the
    reservation instead — this becomes a no-op so the two can't double up."""
    quote = db.get(models.Quote, quote_id)
    if not quote:
        raise ValueError("Quotation not found")
    if quote.converted_orders:
        return quote
    quote.allocate_stock = bool(allocate)
    db.flush()
    _rebuild_quote_reservations(db, quote)
    return quote


def delete_quote(db: Session, quote_id: int) -> None:
    """Admin-only, for cleaning up test/skewed data during the soft
    rollout — a hard delete, not a status change. Items and reservations
    cascade automatically; a quote that's already been converted to an
    order is left alone (the order stands on its own either way)."""
    quote = db.get(models.Quote, quote_id)
    if quote:
        db.delete(quote)
        db.flush()


# --- orders ------------------------------------------------------------------------

def save_order(db: Session, header: dict, items: list[dict], username: str, order_id: int | None = None) -> models.Order:
    prepared, subtotal = _prepare_items(items)
    tax_rate = Decimal(str(header.get("tax_rate", 20) or 0))
    tax_total = money(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax_total
    status = str(header.get("status", "Draft"))
    if status not in ORDER_STATUSES:
        raise ValueError("Invalid order status")

    if order_id:
        order = db.get(models.Order, order_id)
        order.items.clear()
    else:
        order = models.Order(
            order_number=str(header.get("order_number") or next_order_number(db)),
            created_by=username,
        )
        db.add(order)

    order.customer_id = int(header["customer_id"])
    order.project = str(header.get("project", "")).strip()
    order.site_address = str(header.get("site_address", "")).strip()
    order.requested_date = str(header.get("requested_date", "")).strip()
    order.status = status
    order.commercial_notes = str(header.get("commercial_notes", "")).strip()
    order.subtotal = subtotal
    order.tax_rate = tax_rate
    order.tax_total = tax_total
    order.total = total
    if "allocate_stock" in header:
        order.allocate_stock = bool(header["allocate_stock"])
    elif order_id is None:
        order.allocate_stock = True
    db.flush()

    for row in prepared:
        db.add(models.OrderItem(order_id=order.order_id, **row))
    db.flush()

    _rebuild_order_reservations(db, order)
    return order


def _rebuild_order_reservations(db: Session, order: models.Order) -> None:
    order.reservations.clear()
    db.flush()
    if order.status != "Confirmed" or not order.allocate_stock:
        return
    requirements = db.execute(
        select(
            models.Recipe.material_id,
            func.sum(
                models.OrderItem.quantity * models.Recipe.quantity_per_unit
                * (1 + models.Recipe.waste_percent / Decimal("100"))
            ),
        )
        .join(models.OrderItem, models.OrderItem.product_id == models.Recipe.product_id)
        .where(models.OrderItem.order_id == order.order_id)
        .group_by(models.Recipe.material_id)
    ).all()
    for material_id, required in requirements:
        db.add(models.OrderMaterialReservation(
            order_id=order.order_id, material_id=material_id, quantity=required,
        ))
    db.flush()


def set_order_status(db: Session, order_id: int, status: str) -> models.Order:
    if status not in ORDER_STATUSES:
        raise ValueError("Invalid order status")
    order = db.get(models.Order, order_id)
    order.status = status
    db.flush()
    _rebuild_order_reservations(db, order)
    if status == "Completed":
        sync_order_to_xero(db, order, XERO_CLIENT_ID, XERO_CLIENT_SECRET)
    return order


def set_order_allocate_stock(db: Session, order_id: int, allocate: bool) -> models.Order:
    """Same manual failsafe as set_quote_allocate_stock, for Confirmed orders."""
    order = db.get(models.Order, order_id)
    if not order:
        raise ValueError("Order not found")
    order.allocate_stock = bool(allocate)
    db.flush()
    _rebuild_order_reservations(db, order)
    return order


def delete_order(db: Session, order_id: int) -> None:
    """Admin-only, same principle as delete_quote — a hard delete for
    cleaning up test/skewed data. Items, reservations, deliveries and
    their GPS pings all cascade automatically. Note: if this order was
    already pushed to Xero as an invoice, deleting it here does NOT
    remove or void that invoice in Xero — only the Tiger One record."""
    order = db.get(models.Order, order_id)
    if order:
        db.delete(order)
        db.flush()


# --- stock -----------------------------------------------------------------------

def material_balances(db: Session) -> list[dict]:
    quote_reserved = dict(db.execute(
        select(models.MaterialReservation.material_id, func.sum(models.MaterialReservation.quantity))
        .group_by(models.MaterialReservation.material_id)
    ).all())
    order_reserved = dict(db.execute(
        select(models.OrderMaterialReservation.material_id, func.sum(models.OrderMaterialReservation.quantity))
        .group_by(models.OrderMaterialReservation.material_id)
    ).all())
    rows = []
    for material in db.scalars(select(models.Material).where(models.Material.active.is_(True)).order_by(models.Material.name)):
        q_reserved = Decimal(str(quote_reserved.get(material.material_id, 0) or 0))
        o_reserved = Decimal(str(order_reserved.get(material.material_id, 0) or 0))
        on_hand = Decimal(str(material.on_hand))
        rows.append({
            "material_id": material.material_id, "code": material.code, "name": material.name,
            "unit": material.unit, "on_hand": on_hand, "reorder_level": material.reorder_level,
            "quote_reserved": q_reserved, "order_reserved": o_reserved,
            "reserved": q_reserved + o_reserved, "available": on_hand - q_reserved - o_reserved,
        })
    return rows


# --- payloads for documents / API -------------------------------------------------

def quote_payload(db: Session, quote_id: int) -> dict:
    quote = db.get(models.Quote, quote_id)
    if not quote:
        raise ValueError("Quotation not found")
    customer = quote.customer
    return {
        "quote_number": quote.quote_number, "revision": quote.revision,
        "status": quote.status, "project": quote.project, "site_address": quote.site_address,
        "requested_date": quote.requested_date, "validity_days": quote.validity_days,
        "created_at": quote.created_at.isoformat() if quote.created_at else "",
        "commercial_notes": quote.commercial_notes,
        "customer_name": customer.display_name, "customer_type": customer.customer_type,
        "contact_name": customer.contact_name, "telephone": customer.telephone, "mobile": customer.mobile,
        "address_1": customer.address_1, "address_2": customer.address_2, "town": customer.town,
        "postcode": customer.postcode, "payment_terms": customer.payment_terms,
        "subtotal": quote.subtotal, "tax_rate": quote.tax_rate, "tax_total": quote.tax_total,
        "total": quote.total, "allocate_stock": quote.allocate_stock,
        "company_name": get_setting(db, "company_name", "Tiger Concrete Ltd"),
        "company_telephone": get_setting(db, "company_telephone", "0116 298 3234"),
        "company_email": get_setting(db, "company_email", "office@tigerconcrete.co.uk"),
        "company_address": get_setting(db, "company_address", "Leicestershire"),
        "items": [
            {"description": i.description, "quantity": i.quantity, "unit": i.unit,
             "unit_price": i.unit_price, "line_total": i.line_total}
            for i in sorted(quote.items, key=lambda i: i.line_number)
        ],
    }


def order_payload(db: Session, order_id: int) -> dict:
    order = db.get(models.Order, order_id)
    if not order:
        raise ValueError("Order not found")
    customer = order.customer
    return {
        "order_number": order.order_number, "document_number": order.order_number,
        "revision": "A", "status": order.status, "project": order.project,
        "site_address": order.site_address, "requested_date": order.requested_date,
        "created_at": order.created_at.isoformat() if order.created_at else "",
        "commercial_notes": order.commercial_notes,
        "source_quote_number": order.source_quote.quote_number if order.source_quote else "",
        "customer_name": customer.display_name, "customer_type": customer.customer_type,
        "contact_name": customer.contact_name, "telephone": customer.telephone, "mobile": customer.mobile,
        "address_1": customer.address_1, "address_2": customer.address_2, "town": customer.town,
        "postcode": customer.postcode, "payment_terms": customer.payment_terms,
        "subtotal": order.subtotal, "tax_rate": order.tax_rate, "tax_total": order.tax_total,
        "total": order.total, "allocate_stock": order.allocate_stock,
        "company_name": get_setting(db, "company_name", "Tiger Concrete Ltd"),
        "company_telephone": get_setting(db, "company_telephone", "0116 298 3234"),
        "company_email": get_setting(db, "company_email", "office@tigerconcrete.co.uk"),
        "company_address": get_setting(db, "company_address", "Leicestershire"),
        "items": [
            {"description": i.description, "quantity": i.quantity, "unit": i.unit,
             "unit_price": i.unit_price, "line_total": i.line_total}
            for i in sorted(order.items, key=lambda i: i.line_number)
        ],
    }


def get_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.get(models.Setting, key)
    return setting.value if setting else default


def database_summary(db: Session) -> dict:
    result = {
        "commercial_customers": db.scalar(select(func.count()).select_from(models.Customer).where(
            models.Customer.active.is_(True), models.Customer.customer_type == "Commercial")) or 0,
        "private_customers": db.scalar(select(func.count()).select_from(models.Customer).where(
            models.Customer.active.is_(True), models.Customer.customer_type == "Private")) or 0,
        "open_quotes": db.scalar(select(func.count()).select_from(models.Quote).where(
            models.Quote.status.in_(("Draft", "Issued")))) or 0,
        "open_orders": db.scalar(select(func.count()).select_from(models.Order).where(
            models.Order.status.in_(("Draft", "Confirmed")))) or 0,
    }
    result["low_stock"] = sum(1 for row in material_balances(db) if row["available"] <= row["reorder_level"])
    return result


# --- deliveries: POD + tracking (new) ----------------------------------------------

def create_delivery(
    db: Session, order_id: int,
    driver_user_id: int | None = None, driver_name: str = "",
    vehicle_id: int | None = None, vehicle: str = "",
    scheduled_date: date | None = None,
) -> models.Delivery:
    if driver_user_id and not driver_name:
        driver = db.get(models.AppUser, driver_user_id)
        driver_name = driver.full_name if driver else ""
    if vehicle_id and not vehicle:
        vehicle_row = db.get(models.Vehicle, vehicle_id)
        vehicle = vehicle_row.registration if vehicle_row else ""
    delivery = models.Delivery(
        order_id=order_id, driver_user_id=driver_user_id, driver_name=driver_name,
        vehicle_id=vehicle_id, vehicle=vehicle,
        scheduled_date=scheduled_date or datetime.now(timezone.utc).date(),
        status="Scheduled", access_token=new_access_token(),
    )
    db.add(delivery)
    db.flush()
    return delivery


def reassign_delivery(
    db: Session, delivery_id: int,
    driver_user_id: int | None, vehicle_id: int | None,
) -> models.Delivery:
    """Change who's doing a scheduled/en-route delivery — a job handed to
    the wrong driver, a truck that's broken down, etc. Not allowed once
    it's already Delivered (that history shouldn't change retroactively)."""
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery:
        raise ValueError("Delivery not found")
    if delivery.status == "Delivered":
        raise ValueError("Can't reassign a delivery that's already been signed off")
    if driver_user_id:
        driver = db.get(models.AppUser, driver_user_id)
        delivery.driver_user_id = driver_user_id
        delivery.driver_name = driver.full_name if driver else ""
    if vehicle_id:
        vehicle_row = db.get(models.Vehicle, vehicle_id)
        delivery.vehicle_id = vehicle_id
        delivery.vehicle = vehicle_row.registration if vehicle_row else ""
    db.flush()
    return delivery


def get_delivery(db: Session, delivery_id: int) -> models.Delivery | None:
    return db.get(models.Delivery, delivery_id)


def get_delivery_by_token(db: Session, token: str) -> models.Delivery | None:
    return db.scalar(select(models.Delivery).where(models.Delivery.access_token == token))


# --- vehicles ---------------------------------------------------------------------------

def save_vehicle(
    db: Session, registration: str, description: str = "",
    traccar_device_id: str = "", vehicle_id: int | None = None,
) -> models.Vehicle:
    if vehicle_id:
        vehicle = db.get(models.Vehicle, vehicle_id)
        vehicle.registration = registration.strip().upper()
        vehicle.description = description.strip()
        vehicle.traccar_device_id = traccar_device_id.strip() or None
    else:
        vehicle = models.Vehicle(
            registration=registration.strip().upper(), description=description.strip(),
            traccar_device_id=traccar_device_id.strip() or None,
        )
        db.add(vehicle)
    db.flush()
    return vehicle


def list_vehicles(db: Session) -> list[models.Vehicle]:
    return list(db.scalars(
        select(models.Vehicle).where(models.Vehicle.active.is_(True)).order_by(models.Vehicle.registration)
    ))


def deactivate_vehicle(db: Session, vehicle_id: int) -> None:
    vehicle = db.get(models.Vehicle, vehicle_id)
    if vehicle:
        vehicle.active = False
        db.flush()


def deliveries_for_driver(db: Session, driver_user_id: int, include_delivered: bool = False) -> list[models.Delivery]:
    """A driver's own jobs — what their dashboard shows after they log in."""
    query = select(models.Delivery).where(models.Delivery.driver_user_id == driver_user_id)
    if not include_delivered:
        query = query.where(models.Delivery.status != "Delivered", models.Delivery.status != "Cancelled")
    return list(db.scalars(query.order_by(models.Delivery.scheduled_date, models.Delivery.delivery_id)))


def todays_jobs(db: Session, today: str) -> list[dict]:
    """Every order requested for today, with its driver/vehicle if a delivery
    has been scheduled — this is what the home screen shows so the office
    knows every job and every driver allocation for the day at a glance."""
    orders = db.scalars(
        select(models.Order)
        .where(models.Order.requested_date == today, models.Order.status != "Cancelled")
        .order_by(models.Order.order_number)
    ).all()
    jobs = []
    for order in orders:
        delivery = order.deliveries[0] if order.deliveries else None
        summary = ", ".join(f"{i.quantity} {i.unit} {i.description}" for i in order.items) or "—"
        jobs.append({
            "order_number": order.order_number,
            "customer_name": order.customer.display_name,
            "site_address": order.site_address,
            "summary": summary,
            "driver_name": delivery.driver_name if delivery else "",
            "vehicle": delivery.vehicle if delivery else "",
            "status": delivery.status if delivery else order.status,
        })
    return jobs


def record_ping(db: Session, delivery: models.Delivery, latitude: float, longitude: float) -> None:
    db.add(models.LocationPing(delivery_id=delivery.delivery_id, latitude=latitude, longitude=longitude))
    if delivery.status == "Scheduled":
        delivery.status = "En Route"
    db.flush()


def record_pod(
    db: Session, delivery: models.Delivery, signed_by: str,
    signature_path: str, photo_path: str, latitude: float | None, longitude: float | None,
) -> models.Delivery:
    delivery.pod_signed_by = signed_by
    delivery.pod_signature_path = signature_path
    delivery.pod_photo_path = photo_path
    delivery.pod_signed_at = datetime.now(timezone.utc)
    delivery.pod_latitude = latitude
    delivery.pod_longitude = longitude
    delivery.status = "Delivered"
    db.flush()
    return delivery


# --- Xero -----------------------------------------------------------------------------

def get_xero_connection(db: Session) -> models.XeroConnection | None:
    return db.scalar(select(models.XeroConnection).order_by(models.XeroConnection.id.desc()).limit(1))


def save_xero_connection(db: Session, tenant_id: str, tenant_name: str, tokens: dict, connected_by: str) -> models.XeroConnection:
    """Single-tenant app — one Xero organisation connected at a time, so a
    fresh connect replaces whatever was there before."""
    existing = get_xero_connection(db)
    if existing:
        db.delete(existing)
        db.flush()
    connection = models.XeroConnection(
        tenant_id=tenant_id, tenant_name=tenant_name, connected_by=connected_by,
        access_token=tokens["access_token"], refresh_token=tokens["refresh_token"],
        expires_at=tokens["expires_at"],
    )
    db.add(connection)
    db.flush()
    return connection


def update_xero_tokens(db: Session, connection: models.XeroConnection, tokens: dict) -> None:
    connection.access_token = tokens["access_token"]
    connection.refresh_token = tokens["refresh_token"]
    connection.expires_at = tokens["expires_at"]
    db.flush()


def disconnect_xero(db: Session) -> None:
    existing = get_xero_connection(db)
    if existing:
        db.delete(existing)
        db.flush()


def ensure_valid_xero_token(db: Session, client_id: str, client_secret: str) -> models.XeroConnection | None:
    """A connection with a guaranteed-fresh access token, refreshing first
    if it's expired (or close to it). Returns None if not connected at all."""
    connection = get_xero_connection(db)
    if not connection:
        return None
    if xero_client.is_expired(connection.expires_at):
        tokens = xero_client.refresh_tokens(client_id, client_secret, connection.refresh_token)
        update_xero_tokens(db, connection, xero_client.tokens_to_row_fields(tokens))
    return connection


def sync_customer_to_xero(db: Session, customer: models.Customer, client_id: str, client_secret: str) -> None:
    """Best-effort, deliberately swallows errors — a Xero hiccup (or Xero
    simply not being connected) must never stop the office from saving a
    customer. Called right after a customer is created."""
    try:
        connection = ensure_valid_xero_token(db, client_id, client_secret)
        if not connection:
            return
        contact_id = xero_client.find_or_create_contact(connection.access_token, connection.tenant_id, customer)
        customer.xero_contact_id = contact_id
        customer.xero_synced_at = datetime.now(timezone.utc)
        db.flush()
    except Exception:
        pass


def sync_order_to_xero(db: Session, order: models.Order, client_id: str, client_secret: str) -> None:
    """Called when an order becomes Completed. Idempotent (skips if this
    order already has a Xero invoice) and, like the customer push, never
    raises — a failed Xero push doesn't undo marking the order Completed."""
    if order.xero_invoice_id:
        return
    try:
        connection = ensure_valid_xero_token(db, client_id, client_secret)
        if not connection:
            return
        customer = order.customer
        if not customer.xero_contact_id:
            customer.xero_contact_id = xero_client.find_or_create_contact(
                connection.access_token, connection.tenant_id, customer,
            )
            customer.xero_synced_at = datetime.now(timezone.utc)
        invoice_id, invoice_number = xero_client.create_invoice(
            connection.access_token, connection.tenant_id, order, customer.xero_contact_id,
        )
        order.xero_invoice_id = invoice_id
        order.xero_invoice_number = invoice_number
        order.xero_synced_at = datetime.now(timezone.utc)
        db.flush()
    except Exception:
        pass


# --- sales reports ----------------------------------------------------------------------

def sales_report(db: Session, date_from: str, date_to: str) -> dict:
    """Completed orders (i.e. actually delivered) with a requested_date in
    [date_from, date_to] inclusive — both as 'YYYY-MM-DD' strings, matching
    how requested_date is stored. This is what the office's date-range
    report button and its CSV export both run off."""
    orders = list(db.scalars(
        select(models.Order)
        .where(
            models.Order.status == "Completed",
            models.Order.requested_date >= date_from,
            models.Order.requested_date <= date_to,
        )
        .order_by(models.Order.requested_date, models.Order.order_number)
    ))
    subtotal = sum((o.subtotal for o in orders), Decimal("0"))
    tax_total = sum((o.tax_total for o in orders), Decimal("0"))
    total = sum((o.total for o in orders), Decimal("0"))
    return {
        "date_from": date_from, "date_to": date_to, "order_count": len(orders),
        "subtotal": subtotal, "tax_total": tax_total, "total": total,
        "orders": orders,
    }


# --- vehicle checks (daily walkaround, DVSA-style) --------------------------------------

# The standard DVSA HGV daily walkaround checklist, condensed to what
# applies to a rigid mixer truck (no trailer-coupling items). Grouped the
# same way the official guidance groups them: inside the cab, then outside.
# Stored by key in items_json — add/remove/reorder here any time without a
# database migration.
WALKAROUND_CHECKLIST = [
    {"group": "Inside the cab", "checks": [
        ("front_view", "Front view — mirrors, cameras and glass clear and undamaged"),
        ("wipers_washers", "Windscreen wipers and washers working"),
        ("dash_warning_lights", "Dashboard warning lights and gauges working correctly"),
        ("steering", "Steering — no excessive play, doesn't jam"),
        ("horn", "Horn works and is accessible"),
        ("brakes_air", "Brakes and air build-up — no leaks, warning system works"),
        ("seatbelts", "Seatbelts — no cuts or damage, secure and retract properly"),
        ("cab_doors_steps", "Cab, doors and steps secure and safe to use"),
    ]},
    {"group": "Outside the vehicle", "checks": [
        ("lights_indicators", "Lights and indicators all working, lenses clean and correct colour"),
        ("fuel_oil_leaks", "No fuel or oil leaks underneath the vehicle"),
        ("body_wings", "Body, wings and panels secure"),
        ("battery", "Battery secure, good condition, not leaking"),
        ("adblue", "AdBlue / diesel exhaust fluid topped up"),
        ("exhaust_smoke", "No excessive engine exhaust smoke"),
        ("tyres_wheels", "Tyres and wheels — tread, pressure, no damage, all nuts tight"),
        ("number_plate", "Number plate clean, undamaged and correctly displayed"),
        ("reflectors_markings", "Reflectors and markings present, clean and secure"),
    ]},
    {"group": "Concrete equipment", "checks": [
        ("mixer_drum", "Mixer drum / chute condition and operation"),
        ("water_tank", "Water tank / hopper — no leaks, adequate level"),
        ("load_security", "Load and equipment secure"),
    ]},
]


def get_todays_vehicle_check(db: Session, driver_user_id: int, vehicle_id: int) -> models.VehicleCheck | None:
    today = datetime.now(timezone.utc).date()
    return db.scalar(
        select(models.VehicleCheck).where(
            models.VehicleCheck.driver_user_id == driver_user_id,
            models.VehicleCheck.vehicle_id == vehicle_id,
            models.VehicleCheck.check_date == today,
        )
    )


def driver_has_checked_in_today(db: Session, driver_user_id: int) -> bool:
    """Used for the dashboard reminder banner — true if this driver has
    submitted a walkaround check for ANY vehicle today."""
    today = datetime.now(timezone.utc).date()
    return db.scalar(
        select(func.count()).select_from(models.VehicleCheck).where(
            models.VehicleCheck.driver_user_id == driver_user_id,
            models.VehicleCheck.check_date == today,
        )
    ) > 0


def save_vehicle_check(
    db: Session, driver_user_id: int, vehicle_id: int, items: dict,
    defect_notes: str, signed_by: str, signature_path: str,
) -> models.VehicleCheck:
    import json
    has_defects = any(v == "defect" for v in items.values())
    check = models.VehicleCheck(
        driver_user_id=driver_user_id, vehicle_id=vehicle_id,
        check_date=datetime.now(timezone.utc).date(), items_json=json.dumps(items),
        has_defects=has_defects, defect_notes=defect_notes.strip(),
        signed_by=signed_by.strip(), signature_path=signature_path,
    )
    db.add(check)
    db.flush()
    return check


def list_vehicle_checks(db: Session, limit: int = 50) -> list[models.VehicleCheck]:
    return list(db.scalars(
        select(models.VehicleCheck).order_by(models.VehicleCheck.submitted_at.desc()).limit(limit)
    ))


# --- office notifications (delivery completed toast) ------------------------------------

def deliveries_completed_since(db: Session, since: datetime) -> list[models.Delivery]:
    return list(db.scalars(
        select(models.Delivery)
        .where(models.Delivery.status == "Delivered", models.Delivery.pod_signed_at > since)
        .order_by(models.Delivery.pod_signed_at)
    ))


# --- Traccar (live truck tracking) --------------------------------------------------------

def sync_vehicle_positions(db: Session, base_url: str, username: str, password: str) -> int:
    """Pulls current positions from Traccar and updates every vehicle whose
    traccar_device_id matches a registered Traccar device. Returns how many
    vehicles were updated. Best-effort — never raises, same principle as
    the Xero push functions: a Traccar hiccup (or it simply not being
    configured yet) must never break anything else in the app."""
    try:
        devices = traccar_client.get_devices(base_url, username, password)
        positions = traccar_client.get_positions(base_url, username, password)
    except Exception:
        return 0

    # Positions are keyed by Traccar's own internal device id, not the
    # human-friendly identifier typed into Traccar Client on the tablet —
    # the devices list is what bridges the two.
    unique_id_by_internal_id = {d["id"]: d.get("uniqueId") for d in devices}
    position_by_unique_id = {}
    for pos in positions:
        unique_id = unique_id_by_internal_id.get(pos.get("deviceId"))
        if unique_id:
            position_by_unique_id[unique_id] = pos

    vehicles = list(db.scalars(select(models.Vehicle).where(models.Vehicle.traccar_device_id.isnot(None))))
    updated = 0
    for vehicle in vehicles:
        pos = position_by_unique_id.get(vehicle.traccar_device_id)
        if not pos:
            continue
        vehicle.last_latitude = pos.get("latitude")
        vehicle.last_longitude = pos.get("longitude")
        fix_time = pos.get("fixTime")
        try:
            vehicle.last_position_at = datetime.fromisoformat(fix_time.replace("Z", "+00:00")) if fix_time else datetime.now(timezone.utc)
        except ValueError:
            vehicle.last_position_at = datetime.now(timezone.utc)
        updated += 1
    db.flush()
    return updated


# --- driver hours: clock points + time entries -------------------------------------------

ACTIVITY_TYPES = ("Driving", "Yard Work", "Break", "Other")


def create_clock_point(db: Session, name: str) -> models.ClockPoint:
    point = models.ClockPoint(name=name.strip(), token=new_access_token())
    db.add(point)
    db.flush()
    return point


def list_clock_points(db: Session) -> list[models.ClockPoint]:
    return list(db.scalars(
        select(models.ClockPoint).where(models.ClockPoint.active.is_(True)).order_by(models.ClockPoint.name)
    ))


def get_clock_point_by_token(db: Session, token: str) -> models.ClockPoint | None:
    return db.scalar(select(models.ClockPoint).where(models.ClockPoint.token == token))


def deactivate_clock_point(db: Session, clock_point_id: int) -> None:
    point = db.get(models.ClockPoint, clock_point_id)
    if point:
        point.active = False
        db.flush()


def get_active_time_entry(db: Session, driver_user_id: int) -> models.TimeEntry | None:
    return db.scalar(
        select(models.TimeEntry)
        .where(models.TimeEntry.driver_user_id == driver_user_id, models.TimeEntry.ended_at.is_(None))
    )


def start_activity(
    db: Session, driver_user_id: int, activity_type: str,
    clock_point_id: int | None = None, vehicle_id: int | None = None, source: str = "qr_scan",
) -> models.TimeEntry:
    """Closes whatever the driver was doing before (if anything) and opens
    a new entry — a driver is only ever doing one thing at a time, so
    switching activity is just starting the next one."""
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError("Invalid activity type")
    clock_out(db, driver_user_id)  # closes any currently-open entry first
    entry = models.TimeEntry(
        driver_user_id=driver_user_id, activity_type=activity_type,
        clock_point_id=clock_point_id, vehicle_id=vehicle_id, source=source,
    )
    db.add(entry)
    db.flush()
    return entry


def clock_out(db: Session, driver_user_id: int) -> models.TimeEntry | None:
    """Ends whatever the driver's currently clocked into, with nothing new
    started. Harmless no-op if nothing was open."""
    active = get_active_time_entry(db, driver_user_id)
    if active:
        active.ended_at = datetime.now(timezone.utc)
        db.flush()
    return active


def time_entries_for_driver(db: Session, driver_user_id: int, date_from: str, date_to: str) -> list[models.TimeEntry]:
    """date_from/date_to are 'YYYY-MM-DD' strings, inclusive, compared
    against when each entry started."""
    start = datetime.fromisoformat(date_from)
    end = datetime.fromisoformat(date_to) + timedelta(days=1)
    return list(db.scalars(
        select(models.TimeEntry)
        .where(models.TimeEntry.driver_user_id == driver_user_id,
               models.TimeEntry.started_at >= start, models.TimeEntry.started_at < end)
        .order_by(models.TimeEntry.started_at)
    ))


def hours_summary(entries: list[models.TimeEntry]) -> dict:
    """Raw totals only, by activity type — deliberately no compliance
    threshold checking here (see the module docstring in models.py):
    which specific hour limits legally apply depends on vehicle type and
    exemptions that need confirming, not something to hard-code blind."""
    totals = {activity: timedelta() for activity in ACTIVITY_TYPES}
    now = datetime.now(timezone.utc)
    for entry in entries:
        started = entry.started_at if entry.started_at.tzinfo else entry.started_at.replace(tzinfo=timezone.utc)
        ended = entry.ended_at
        if ended and not ended.tzinfo:
            ended = ended.replace(tzinfo=timezone.utc)
        duration = (ended or now) - started
        totals[entry.activity_type] = totals.get(entry.activity_type, timedelta()) + duration
    return {activity: round(td.total_seconds() / 3600, 2) for activity, td in totals.items()}


def list_all_drivers_time_status(db: Session) -> list[dict]:
    """Office overview — every driver and what they're currently clocked
    into (or not), for a quick 'who's doing what right now' view."""
    drivers = list_drivers(db)
    result = []
    for driver in drivers:
        active = get_active_time_entry(db, driver.user_id)
        result.append({
            "driver": driver, "active_entry": active,
            "since": active.started_at if active else None,
        })
    return result


# --- tachograph records (office-verified driving hours) --------------------------------

def save_tachograph_record(
    db: Session, driver_user_id: int, record_date, driving_hours, entered_by: str,
    vehicle_id: int | None = None, notes: str = "", source_reference: str = "",
) -> models.TachographRecord:
    """One record per driver per day — re-entering the same date updates
    it in place (e.g. a correction after re-checking the chart) rather
    than creating a duplicate."""
    existing = db.scalar(
        select(models.TachographRecord).where(
            models.TachographRecord.driver_user_id == driver_user_id,
            models.TachographRecord.record_date == record_date,
        )
    )
    if existing:
        existing.driving_hours = driving_hours
        existing.vehicle_id = vehicle_id
        existing.notes = notes.strip()
        existing.source_reference = source_reference.strip()
        existing.entered_by = entered_by
        db.flush()
        return existing
    record = models.TachographRecord(
        driver_user_id=driver_user_id, vehicle_id=vehicle_id, record_date=record_date,
        driving_hours=driving_hours, notes=notes.strip(), source_reference=source_reference.strip(),
        entered_by=entered_by,
    )
    db.add(record)
    db.flush()
    return record


def tachograph_records_for_driver(db: Session, driver_user_id: int, date_from: str, date_to: str) -> list[models.TachographRecord]:
    start = datetime.fromisoformat(date_from).date()
    end = datetime.fromisoformat(date_to).date()
    return list(db.scalars(
        select(models.TachographRecord)
        .where(models.TachographRecord.driver_user_id == driver_user_id,
               models.TachographRecord.record_date >= start, models.TachographRecord.record_date <= end)
        .order_by(models.TachographRecord.record_date)
    ))


def delete_tachograph_record(db: Session, record_id: int) -> None:
    record = db.get(models.TachographRecord, record_id)
    if record:
        db.delete(record)
        db.flush()
