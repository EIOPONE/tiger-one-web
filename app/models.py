"""Tiger One (web) — SQLAlchemy data model.

This is a direct port of the schema from the original Tiger One desktop
app's database.py. Table names, columns and constraints are kept the same
on purpose so the business logic (recipes, reservations, quote/order
status rules) carries over unchanged. The only real differences are:
  - runs on Postgres in production (SQLite still works for local dev/tests)
  - password hashing uses bcrypt instead of raw SHA-256
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric,
    String, UniqueConstraint, Boolean, func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class AppUser(Base):
    __tablename__ = "app_users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="Sales")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"

    customer_id = Column(Integer, primary_key=True)
    customer_type = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    contact_name = Column(String, nullable=False, default="")
    telephone = Column(String, nullable=False, default="")
    mobile = Column(String, nullable=False, default="")
    email = Column(String, nullable=False, default="")
    address_1 = Column(String, nullable=False, default="")
    address_2 = Column(String, nullable=False, default="")
    town = Column(String, nullable=False, default="")
    postcode = Column(String, nullable=False, default="")
    payment_terms = Column(String, nullable=False, default="")
    notes = Column(String, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("customer_type IN ('Commercial','Private')", name="ck_customer_type"),
    )


class Material(Base):
    __tablename__ = "materials"

    material_id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    unit = Column(String, nullable=False)
    on_hand = Column(Numeric(14, 3), nullable=False, default=0)
    reorder_level = Column(Numeric(14, 3), nullable=False, default=0)
    reorder_quantity = Column(Numeric(14, 3), nullable=False, default=0)
    unit_cost = Column(Numeric(14, 4), nullable=False, default=0)
    supplier = Column(String, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class Product(Base):
    __tablename__ = "products"

    product_id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=False, default="")
    sell_unit = Column(String, nullable=False, default="m³")
    default_unit_price = Column(Numeric(14, 4), nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    recipes = relationship("Recipe", cascade="all, delete-orphan", backref="product")


class Recipe(Base):
    __tablename__ = "recipes"

    recipe_id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.material_id"), nullable=False)
    quantity_per_unit = Column(Numeric(14, 4), nullable=False)
    waste_percent = Column(Numeric(6, 2), nullable=False, default=0)

    material = relationship("Material")

    __table_args__ = (
        UniqueConstraint("product_id", "material_id", name="uq_recipe_product_material"),
        CheckConstraint("quantity_per_unit >= 0", name="ck_recipe_qty"),
        CheckConstraint("waste_percent >= 0", name="ck_recipe_waste"),
    )


class Quote(Base):
    __tablename__ = "quotes"

    quote_id = Column(Integer, primary_key=True)
    quote_number = Column(String, nullable=False, unique=True)
    revision = Column(String, nullable=False, default="A")
    customer_id = Column(Integer, ForeignKey("customers.customer_id"), nullable=False)
    project = Column(String, nullable=False, default="")
    site_address = Column(String, nullable=False, default="")
    requested_date = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="Draft")
    validity_days = Column(Integer, nullable=False, default=14)
    commercial_notes = Column(String, nullable=False, default="")
    subtotal = Column(Numeric(14, 2), nullable=False, default=0)
    tax_rate = Column(Numeric(6, 2), nullable=False, default=20)
    tax_total = Column(Numeric(14, 2), nullable=False, default=0)
    total = Column(Numeric(14, 2), nullable=False, default=0)
    # Manual failsafe: an Accepted quote only reserves stock if this is True.
    # Lets the office leave stock free for jobs happening in the next day or
    # two even once a further-out quote has been accepted.
    allocate_stock = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    customer = relationship("Customer")
    items = relationship("QuoteItem", cascade="all, delete-orphan", backref="quote")
    reservations = relationship("MaterialReservation", cascade="all, delete-orphan", backref="quote")
    converted_orders = relationship("Order", back_populates="source_quote")

    __table_args__ = (
        CheckConstraint(
            "status IN ('Draft','Issued','Accepted','Lost','Cancelled')", name="ck_quote_status"
        ),
    )


class QuoteItem(Base):
    __tablename__ = "quote_items"

    quote_item_id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, ForeignKey("quotes.quote_id", ondelete="CASCADE"), nullable=False)
    line_number = Column(Integer, nullable=False)
    product_id = Column(Integer, ForeignKey("products.product_id"))
    description = Column(String, nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(String, nullable=False, default="m³")
    unit_price = Column(Numeric(14, 2), nullable=False, default=0)
    line_total = Column(Numeric(14, 2), nullable=False, default=0)

    product = relationship("Product")

    __table_args__ = (CheckConstraint("quantity >= 0", name="ck_quote_item_qty"),)


class MaterialReservation(Base):
    __tablename__ = "material_reservations"

    reservation_id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, ForeignKey("quotes.quote_id", ondelete="CASCADE"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.material_id"), nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("quote_id", "material_id", name="uq_reservation_quote_material"),
        CheckConstraint("quantity >= 0", name="ck_reservation_qty"),
    )


class Order(Base):
    __tablename__ = "orders"

    order_id = Column(Integer, primary_key=True)
    order_number = Column(String, nullable=False, unique=True)
    customer_id = Column(Integer, ForeignKey("customers.customer_id"), nullable=False)
    project = Column(String, nullable=False, default="")
    site_address = Column(String, nullable=False, default="")
    requested_date = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="Draft")
    commercial_notes = Column(String, nullable=False, default="")
    subtotal = Column(Numeric(14, 2), nullable=False, default=0)
    tax_rate = Column(Numeric(6, 2), nullable=False, default=20)
    tax_total = Column(Numeric(14, 2), nullable=False, default=0)
    total = Column(Numeric(14, 2), nullable=False, default=0)
    # Same failsafe as Quote.allocate_stock — a Confirmed order only reserves
    # stock if this is True.
    allocate_stock = Column(Boolean, nullable=False, default=True)
    # Set automatically when this order was created by accepting a quote —
    # keeps the paper trail from quotation through to live job.
    source_quote_id = Column(Integer, ForeignKey("quotes.quote_id"), nullable=True)
    created_by = Column(String, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    customer = relationship("Customer")
    source_quote = relationship("Quote", back_populates="converted_orders")
    items = relationship("OrderItem", cascade="all, delete-orphan", backref="order")
    reservations = relationship("OrderMaterialReservation", cascade="all, delete-orphan", backref="order")
    deliveries = relationship("Delivery", back_populates="order")

    __table_args__ = (
        CheckConstraint("status IN ('Draft','Confirmed','Completed','Cancelled')", name="ck_order_status"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    order_item_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    line_number = Column(Integer, nullable=False)
    product_id = Column(Integer, ForeignKey("products.product_id"))
    description = Column(String, nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(String, nullable=False, default="m³")
    unit_price = Column(Numeric(14, 2), nullable=False, default=0)
    line_total = Column(Numeric(14, 2), nullable=False, default=0)

    product = relationship("Product")

    __table_args__ = (CheckConstraint("quantity >= 0", name="ck_order_item_qty"),)


class OrderMaterialReservation(Base):
    __tablename__ = "order_material_reservations"

    reservation_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.material_id"), nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("order_id", "material_id", name="uq_order_reservation_material"),
        CheckConstraint("quantity >= 0", name="ck_order_reservation_qty"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False, default="")


# --- New for the web/field build -------------------------------------------------

class Delivery(Base):
    """One truck run against a confirmed order — the anchor for POD + tracking."""
    __tablename__ = "deliveries"

    delivery_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id"), nullable=False)
    driver_name = Column(String, nullable=False, default="")
    vehicle = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="Scheduled")
    access_token = Column(String, nullable=False, unique=True)  # driver's link, no login needed
    pod_signed_by = Column(String, nullable=False, default="")
    pod_signature_path = Column(String, nullable=False, default="")
    pod_photo_path = Column(String, nullable=False, default="")
    pod_signed_at = Column(DateTime, nullable=True)
    pod_latitude = Column(Numeric(9, 6), nullable=True)
    pod_longitude = Column(Numeric(9, 6), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    order = relationship("Order", back_populates="deliveries")
    pings = relationship("LocationPing", cascade="all, delete-orphan", backref="delivery")

    __table_args__ = (
        CheckConstraint(
            "status IN ('Scheduled','En Route','Delivered','Cancelled')", name="ck_delivery_status"
        ),
    )


class LocationPing(Base):
    """A single GPS ping from a driver's delivery page while a run is in progress."""
    __tablename__ = "location_pings"

    ping_id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.delivery_id", ondelete="CASCADE"), nullable=False)
    latitude = Column(Numeric(9, 6), nullable=False)
    longitude = Column(Numeric(9, 6), nullable=False)
    recorded_at = Column(DateTime, nullable=False, server_default=func.now())
