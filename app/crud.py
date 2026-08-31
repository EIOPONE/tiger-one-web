"""Business rules — recipes, allocation, quote/order lifecycle.

This mirrors the original Tiger One desktop database.py function-for-function
where possible, so the same behaviour (and the same self-tests, ported into
tests/test_business_rules.py) carry over unchanged.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .security import hash_password, verify_password, new_access_token

TWOPLACES = Decimal("0.01")
QUOTE_STATUSES = ("Draft", "Issued", "Accepted", "Lost", "Cancelled")
ORDER_STATUSES = ("Draft", "Confirmed", "Completed", "Cancelled")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# --- users -------------------------------------------------------------------

def ensure_admin_user(db: Session) -> None:
    existing = db.scalar(select(models.AppUser).where(models.AppUser.username == "admin"))
    if existing:
        return
    db.add(models.AppUser(
        username="admin", password_hash=hash_password("tigerone"),
        full_name="Daniel Anderson", role="Commercial Manager",
    ))
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
    if customer_id:
        customer = db.get(models.Customer, customer_id)
        for key, value in values.items():
            setattr(customer, key, value)
    else:
        customer = models.Customer(**values)
        db.add(customer)
    db.flush()
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
    db: Session, order_id: int, vehicle: str,
    driver_user_id: int | None = None, driver_name: str = "",
    scheduled_date: date | None = None,
) -> models.Delivery:
    if driver_user_id and not driver_name:
        driver = db.get(models.AppUser, driver_user_id)
        driver_name = driver.full_name if driver else ""
    delivery = models.Delivery(
        order_id=order_id, driver_user_id=driver_user_id, driver_name=driver_name,
        vehicle=vehicle, scheduled_date=scheduled_date or datetime.now(timezone.utc).date(),
        status="Scheduled", access_token=new_access_token(),
    )
    db.add(delivery)
    db.flush()
    return delivery


def get_delivery(db: Session, delivery_id: int) -> models.Delivery | None:
    return db.get(models.Delivery, delivery_id)


def get_delivery_by_token(db: Session, token: str) -> models.Delivery | None:
    return db.scalar(select(models.Delivery).where(models.Delivery.access_token == token))


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
