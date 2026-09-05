from __future__ import annotations

import os
import asyncio
import io
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import crud, models
from . import pod_pdf, xero_client, report_pdf, quote_pdf
from .database import get_session, init_db

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOGO_PATH = BASE_DIR / "branding" / "tiger_concrete_logo.jpg"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
signer = URLSafeSerializer(SECRET_KEY, salt="tiger-one-session")
XERO_CLIENT_ID = os.environ.get("XERO_CLIENT_ID", "")
XERO_CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET", "")
XERO_REDIRECT_URI = os.environ.get("XERO_REDIRECT_URI", "http://127.0.0.1:8000/xero/callback")
TRACCAR_URL = os.environ.get("TRACCAR_URL", "")
TRACCAR_USERNAME = os.environ.get("TRACCAR_USERNAME", "")
TRACCAR_PASSWORD = os.environ.get("TRACCAR_PASSWORD", "")

app = FastAPI(title="Tiger One")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    init_db()
    with get_session() as db:
        crud.ensure_admin_user(db)
        crud.backfill_vehicle_qr_tokens(db)
    if TRACCAR_URL and TRACCAR_USERNAME and TRACCAR_PASSWORD:
        asyncio.create_task(_traccar_poll_loop())


async def _traccar_poll_loop():
    """Pulls positions from Traccar every 30s and updates the matching
    vehicles. Runs for the lifetime of the app; only starts at all if
    TRACCAR_URL/USERNAME/PASSWORD are set, so it's a complete no-op — not
    even a background task — until Traccar's actually configured."""
    while True:
        try:
            def _sync():
                with get_session() as db:
                    return crud.sync_vehicle_positions(db, TRACCAR_URL, TRACCAR_USERNAME, TRACCAR_PASSWORD)
            await asyncio.to_thread(_sync)
        except Exception:
            pass  # never let a bad poll cycle kill the loop
        await asyncio.sleep(30)


def db_dependency():
    with get_session() as db:
        yield db


def current_user(request: Request, db: Session = Depends(db_dependency)) -> models.AppUser:
    cookie = request.cookies.get("session")
    if not cookie:
        raise HTTPException(status_code=401, detail="Not signed in")
    try:
        data = signer.loads(cookie)
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = db.get(models.AppUser, data.get("user_id"))
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def get_user_or_none(request: Request, db: Session) -> models.AppUser | None:
    """Same check as current_user, but for page routes that want to redirect
    to /login instead of returning a JSON 401."""
    cookie = request.cookies.get("session")
    if not cookie:
        return None
    try:
        data = signer.loads(cookie)
    except BadSignature:
        return None
    user = db.get(models.AppUser, data.get("user_id"))
    return user if (user and user.active) else None


def products_json(db: Session) -> str:
    import json
    products = db.query(models.Product).filter(models.Product.active.is_(True)).all()
    return json.dumps([
        {"product_id": p.product_id, "name": p.name, "default_unit_price": float(p.default_unit_price),
         "sell_unit": p.sell_unit} for p in products
    ])


def materials_json(db: Session) -> str:
    import json
    materials = db.query(models.Material).filter(models.Material.active.is_(True)).all()
    return json.dumps([
        {"material_id": m.material_id, "name": m.name, "unit": m.unit} for m in materials
    ])


# --- auth ---------------------------------------------------------------------------

class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(payload: LoginPayload, db: Session = Depends(db_dependency)):
    user = crud.authenticate(db, payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = signer.dumps({"user_id": user.user_id})
    response = JSONResponse({"user": user.full_name, "role": user.role})
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


# --- customers ------------------------------------------------------------------------

@app.get("/api/customers")
def list_customers(db: Session = Depends(db_dependency), user=Depends(current_user)):
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).all()
    return [{"customer_id": c.customer_id, "display_name": c.display_name,
             "customer_type": c.customer_type} for c in customers]


@app.post("/api/customers")
def create_customer(values: dict, db: Session = Depends(db_dependency), user=Depends(current_user)):
    customer = crud.save_customer(db, values)
    return {"customer_id": customer.customer_id}


# --- materials -------------------------------------------------------------------------

@app.get("/api/materials")
def list_materials(db: Session = Depends(db_dependency), user=Depends(current_user)):
    return crud.material_balances(db)


@app.post("/api/materials/{material_id}/receive")
def receive_stock(material_id: int, quantity: float, db: Session = Depends(db_dependency), user=Depends(current_user)):
    crud.receive_stock(db, material_id, quantity)
    return {"ok": True}


# --- quotes ----------------------------------------------------------------------------

@app.post("/api/quotes")
def create_quote(payload: dict, db: Session = Depends(db_dependency), user=Depends(current_user)):
    quote = crud.save_quote(db, payload["header"], payload["items"], user.full_name)
    return {"quote_id": quote.quote_id, "quote_number": quote.quote_number, "total": str(quote.total)}


@app.post("/api/quotes/{quote_id}/status")
def update_quote_status(quote_id: int, status: str, db: Session = Depends(db_dependency), user=Depends(current_user)):
    quote = crud.set_quote_status(db, quote_id, status)
    return {"quote_id": quote.quote_id, "status": quote.status}


@app.get("/api/quotes/{quote_id}")
def get_quote(quote_id: int, db: Session = Depends(db_dependency), user=Depends(current_user)):
    return crud.quote_payload(db, quote_id)


@app.post("/api/quotes/{quote_id}/allocate-stock")
def set_quote_allocate_stock(quote_id: int, allocate: bool, db: Session = Depends(db_dependency), user=Depends(current_user)):
    """The 'allocate stock: yes/no' failsafe toggle for an Accepted quote."""
    quote = crud.set_quote_allocate_stock(db, quote_id, allocate)
    return {"quote_id": quote.quote_id, "allocate_stock": quote.allocate_stock}


# --- orders + deliveries ----------------------------------------------------------------

@app.post("/api/orders")
def create_order(payload: dict, db: Session = Depends(db_dependency), user=Depends(current_user)):
    order = crud.save_order(db, payload["header"], payload["items"], user.full_name)
    return {"order_id": order.order_id, "order_number": order.order_number, "total": str(order.total)}


@app.post("/api/orders/{order_id}/status")
def update_order_status(order_id: int, status: str, db: Session = Depends(db_dependency), user=Depends(current_user)):
    order = crud.set_order_status(db, order_id, status)
    return {"order_id": order.order_id, "status": order.status}


@app.post("/api/orders/{order_id}/allocate-stock")
def set_order_allocate_stock(order_id: int, allocate: bool, db: Session = Depends(db_dependency), user=Depends(current_user)):
    """The 'allocate stock: yes/no' failsafe toggle for a Confirmed order."""
    order = crud.set_order_allocate_stock(db, order_id, allocate)
    return {"order_id": order.order_id, "allocate_stock": order.allocate_stock}


@app.post("/api/orders/{order_id}/deliveries")
def schedule_delivery(order_id: int, driver_name: str, vehicle: str,
                       db: Session = Depends(db_dependency), user=Depends(current_user)):
    delivery = crud.create_delivery(db, order_id, driver_name=driver_name, vehicle=vehicle)
    return {"delivery_id": delivery.delivery_id, "driver_link": f"/d/{delivery.access_token}"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(db_dependency), user=Depends(current_user)):
    return crud.database_summary(db)


# --- Web UI (office screens) ------------------------------------------------------------

@app.get("/sw.js")
def service_worker():
    """Served from the root (not /static/) so its scope covers /driver/*
    — a service worker can only control pages under the path it's served
    from. This is what makes 'Add to Home Screen' produce a real
    standalone app on Android instead of just a bookmark shortcut."""
    sw_path = BASE_DIR / "static" / "sw.js"
    return Response(content=sw_path.read_text(), media_type="application/javascript")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(db_dependency)):
    if get_user_or_none(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                  db: Session = Depends(db_dependency)):
    user = crud.authenticate(db, username, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {
            "user": None, "error": "Incorrect username or password",
        }, status_code=401)
    token = signer.dumps({"user_id": user.user_id})
    response = RedirectResponse("/driver" if user.role == "Driver" else "/", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/web-logout")
def web_logout(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    destination = "/driver/login" if (user and user.role == "Driver") else "/login"
    response = RedirectResponse(destination, status_code=303)
    response.delete_cookie("session")
    return response


def require_office_user(request: Request, db: Session):
    """Office pages: signed-in AND not a Driver account (drivers get their own
    dashboard). Returns the user, or a RedirectResponse to send back instead."""
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role == "Driver":
        return RedirectResponse("/driver", status_code=303)
    return user


def require_admin_user(request: Request, db: Session):
    """Admin-only actions — staff management and deleting quotes/orders.
    Returns the user, or a RedirectResponse to send back instead."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role != "Admin":
        return RedirectResponse("/", status_code=303)
    return user


@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "active": "dashboard", "summary": crud.database_summary(db),
        "jobs_today": crud.todays_jobs(db, today),
        "today_label": datetime.now().strftime("%A %d %B %Y"),
    })


@app.get("/api/dashboard/live")
def dashboard_live(request: Request, db: Session = Depends(db_dependency)):
    """Polled by the dashboard page so today's jobs and the KPI counts stay
    current without a manual refresh — e.g. the moment a driver signs off
    a delivery, its status updates here on its own."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    today = datetime.now().strftime("%Y-%m-%d")
    return {"summary": crud.database_summary(db), "jobs_today": crud.todays_jobs(db, today)}


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    impacts = {c.customer_id: crud.customer_delete_impact(db, c.customer_id) for c in customers}
    return templates.TemplateResponse(request, "customers.html", {
        "user": user, "active": "customers", "customers": customers, "impacts": impacts,
    })


@app.post("/customers/{customer_id}/delete")
def customers_delete(request: Request, customer_id: int, db: Session = Depends(db_dependency)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.delete_customer(db, customer_id)
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/new")
def customers_new(
    request: Request, customer_type: str = Form(...), display_name: str = Form(...),
    contact_name: str = Form(""), mobile: str = Form(""), email: str = Form(""),
    payment_terms: str = Form(""), address_1: str = Form(""), db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.save_customer(db, {
        "customer_type": customer_type, "display_name": display_name, "contact_name": contact_name,
        "mobile": mobile, "email": email, "payment_terms": payment_terms, "address_1": address_1,
    })
    return RedirectResponse("/customers", status_code=303)


@app.get("/materials", response_class=HTMLResponse)
def materials_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "materials.html", {
        "user": user, "active": "materials", "materials": crud.material_balances(db),
    })


@app.post("/materials/new")
def materials_new(
    request: Request, code: str = Form(...), name: str = Form(...), unit: str = Form(...),
    reorder_level: float = Form(0), reorder_quantity: float = Form(0), unit_cost: float = Form(0),
    supplier: str = Form(""), db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.save_material(db, {
        "code": code, "name": name, "unit": unit, "on_hand": 0, "reorder_level": reorder_level,
        "reorder_quantity": reorder_quantity, "unit_cost": unit_cost, "supplier": supplier,
    })
    return RedirectResponse("/materials", status_code=303)


@app.post("/materials/{material_id}/receive")
def materials_receive(request: Request, material_id: int, quantity: float = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.receive_stock(db, material_id, quantity)
    return RedirectResponse("/materials", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    products = db.query(models.Product).filter(models.Product.active.is_(True)).order_by(models.Product.name).all()
    return templates.TemplateResponse(request, "products.html", {
        "user": user, "active": "products", "products": products, "materials_json": materials_json(db),
    })


@app.post("/products/new")
async def products_new(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    material_ids = form.getlist("material_id")
    quantities = form.getlist("quantity_per_unit")
    wastes = form.getlist("waste_percent")
    recipe_lines = [
        {"material_id": mid, "quantity_per_unit": qty, "waste_percent": waste or 0}
        for mid, qty, waste in zip(material_ids, quantities, wastes) if mid and qty
    ]
    crud.save_product(db, {
        "code": form["code"], "name": form["name"], "description": "",
        "sell_unit": form.get("sell_unit") or "m³",
        "default_unit_price": float(form.get("default_unit_price") or 0),
    }, recipe_lines)
    return RedirectResponse("/products", status_code=303)


@app.get("/quotes", response_class=HTMLResponse)
def quotes_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    quotes = db.query(models.Quote).order_by(models.Quote.created_at.desc()).all()
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    return templates.TemplateResponse(request, "quotes.html", {
        "user": user, "active": "quotes", "quotes": quotes, "customers": customers,
        "products_json": products_json(db),
    })


@app.post("/quotes/new")
async def quotes_new(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    product_ids = form.getlist("product_id")
    descriptions = form.getlist("description")
    quantities = form.getlist("quantity")
    units = form.getlist("unit")
    unit_prices = form.getlist("unit_price")
    items = [
        {"product_id": pid or None, "description": desc, "quantity": qty, "unit": unit, "unit_price": price}
        for pid, desc, qty, unit, price in zip(product_ids, descriptions, quantities, units, unit_prices)
        if desc and qty
    ]
    crud.save_quote(db, {
        "customer_id": form["customer_id"], "project": form.get("project", ""),
        "site_address": form.get("site_address", ""), "requested_date": form.get("requested_date", ""),
        "status": form.get("status", "Draft"), "tax_rate": float(form.get("tax_rate") or 20),
        "allocate_stock": form.get("allocate_stock") == "true",
    }, items, user.full_name)
    return RedirectResponse("/quotes", status_code=303)


@app.post("/quotes/{quote_id}/status")
def quotes_status(request: Request, quote_id: int, status: str = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.set_quote_status(db, quote_id, status)
    return RedirectResponse("/quotes", status_code=303)


@app.post("/quotes/{quote_id}/allocate")
def quotes_allocate(request: Request, quote_id: int, allocate: str = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.set_quote_allocate_stock(db, quote_id, allocate == "true")
    return RedirectResponse("/quotes", status_code=303)


@app.post("/quotes/{quote_id}/delete")
def quotes_delete(request: Request, quote_id: int, db: Session = Depends(db_dependency)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.delete_quote(db, quote_id)
    return RedirectResponse("/quotes", status_code=303)


@app.get("/quotes/{quote_id}/pdf")
def quote_pdf_route(quote_id: int, request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    payload = crud.quote_payload(db, quote_id)
    pdf_bytes = quote_pdf.generate_quote_or_order_pdf(payload, LOGO_PATH, is_order=False)
    filename = f"Quotation_{payload['quote_number'].replace(' ', '_')}_Rev_{payload['revision']}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    orders = db.query(models.Order).order_by(models.Order.created_at.desc()).all()
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    return templates.TemplateResponse(request, "orders.html", {
        "user": user, "active": "orders", "orders": orders, "customers": customers,
        "products_json": products_json(db), "drivers": crud.list_drivers(db),
        "vehicles": crud.list_vehicles(db),
    })


@app.post("/orders/new")
async def orders_new(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    product_ids = form.getlist("product_id")
    descriptions = form.getlist("description")
    quantities = form.getlist("quantity")
    units = form.getlist("unit")
    unit_prices = form.getlist("unit_price")
    items = [
        {"product_id": pid or None, "description": desc, "quantity": qty, "unit": unit, "unit_price": price}
        for pid, desc, qty, unit, price in zip(product_ids, descriptions, quantities, units, unit_prices)
        if desc and qty
    ]
    crud.save_order(db, {
        "customer_id": form["customer_id"], "project": form.get("project", ""),
        "site_address": form.get("site_address", ""), "requested_date": form.get("requested_date", ""),
        "status": form.get("status", "Draft"), "tax_rate": float(form.get("tax_rate") or 20),
        "allocate_stock": form.get("allocate_stock") == "true",
    }, items, user.full_name)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/status")
def orders_status(request: Request, order_id: int, status: str = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.set_order_status(db, order_id, status)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/xero-retry")
def orders_xero_retry(request: Request, order_id: int, db: Session = Depends(db_dependency)):
    """Visible retry for when the automatic push to Xero failed (outage,
    Xero temporarily unreachable, etc) — sync_order_to_xero is a no-op if
    it's already succeeded, so this is always safe to click."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    order = db.get(models.Order, order_id)
    if order:
        crud.sync_order_to_xero(db, order, crud.XERO_CLIENT_ID, crud.XERO_CLIENT_SECRET)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/allocate")
def orders_allocate(request: Request, order_id: int, allocate: str = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.set_order_allocate_stock(db, order_id, allocate == "true")
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/delete")
def orders_delete(request: Request, order_id: int, db: Session = Depends(db_dependency)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.delete_order(db, order_id)
    return RedirectResponse("/orders", status_code=303)


@app.get("/orders/{order_id}/pdf")
def order_pdf(order_id: int, request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    payload = crud.order_payload(db, order_id)
    pdf_bytes = quote_pdf.generate_quote_or_order_pdf(payload, LOGO_PATH, is_order=True)
    filename = f"Order_Confirmation_{payload['order_number'].replace(' ', '_')}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


@app.post("/orders/{order_id}/deliveries")
def orders_schedule_delivery_page(
    request: Request, order_id: int,
    driver_user_id: str = Form(""), vehicle_id: str = Form(""), scheduled_date: str = Form(""),
    db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    parsed_date = date.fromisoformat(scheduled_date) if scheduled_date else None
    crud.create_delivery(
        db, order_id,
        driver_user_id=int(driver_user_id) if driver_user_id else None,
        vehicle_id=int(vehicle_id) if vehicle_id else None,
        scheduled_date=parsed_date,
    )
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/deliveries/{delivery_id}/reassign")
def orders_reassign_delivery(
    request: Request, order_id: int, delivery_id: int,
    driver_user_id: str = Form(""), vehicle_id: str = Form(""),
    db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        crud.reassign_delivery(
            db, delivery_id,
            driver_user_id=int(driver_user_id) if driver_user_id else None,
            vehicle_id=int(vehicle_id) if vehicle_id else None,
        )
    except ValueError:
        pass  # e.g. tried to reassign an already-Delivered run — silently ignored, nothing to do
    return RedirectResponse("/orders", status_code=303)


# --- office staff accounts (Admin only) ---------------------------------------------------

@app.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "staff.html", {
        "user": user, "active": "staff", "staff": crud.list_office_users(db),
    })


@app.post("/staff/new")
def staff_new(
    request: Request, full_name: str = Form(...), username: str = Form(...),
    password: str = Form(...), role: str = Form(...), db: Session = Depends(db_dependency),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.create_office_user(db, full_name, username, password, role)
    return RedirectResponse("/staff", status_code=303)


@app.post("/staff/{staff_user_id}/deactivate")
def staff_deactivate(request: Request, staff_user_id: int, db: Session = Depends(db_dependency)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.deactivate_office_user(db, staff_user_id)
    return RedirectResponse("/staff", status_code=303)


# --- drivers (office admin) ---------------------------------------------------------------


@app.get("/drivers", response_class=HTMLResponse)
def drivers_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "drivers.html", {
        "user": user, "active": "drivers", "drivers": crud.list_drivers(db),
    })


@app.post("/drivers/new")
def drivers_new(
    request: Request, full_name: str = Form(...), username: str = Form(...), pin: str = Form(...),
    db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.create_driver(db, full_name, username, pin)
    return RedirectResponse("/drivers", status_code=303)


@app.post("/drivers/{driver_user_id}/deactivate")
def drivers_deactivate(request: Request, driver_user_id: int, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.deactivate_driver(db, driver_user_id)
    return RedirectResponse("/drivers", status_code=303)


# --- vehicles (office admin) ----------------------------------------------------------------

@app.get("/vehicles", response_class=HTMLResponse)
def vehicles_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicles = crud.list_vehicles(db)
    current_drivers = {v.vehicle_id: crud.get_active_driver_for_vehicle(db, v.vehicle_id) for v in vehicles}
    return templates.TemplateResponse(request, "vehicles.html", {
        "user": user, "active": "vehicles", "vehicles": vehicles, "current_drivers": current_drivers,
    })


@app.get("/vehicles/{vehicle_id}/qr.png")
def vehicles_qr(request: Request, vehicle_id: int, db: Session = Depends(db_dependency)):
    """The cab QR code — points at /driver/clock/vehicle/{token}."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = db.get(models.Vehicle, vehicle_id)
    if not vehicle or not vehicle.qr_token:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    import qrcode
    clock_url = str(request.base_url).rstrip("/") + f"/driver/clock/vehicle/{vehicle.qr_token}"
    img = qrcode.make(clock_url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/vehicles/map", response_class=HTMLResponse)
def vehicles_map_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "vehicles_map.html", {
        "user": user, "active": "vehicles",
    })


@app.get("/api/vehicles/positions")
def vehicles_positions(request: Request, db: Session = Depends(db_dependency)):
    """Feeds the fleet map — polled every ~20s from the browser so pins
    move without a page reload."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicles = crud.list_vehicles(db)
    return [{
        "vehicle_id": v.vehicle_id, "registration": v.registration, "description": v.description,
        "latitude": float(v.last_latitude) if v.last_latitude is not None else None,
        "longitude": float(v.last_longitude) if v.last_longitude is not None else None,
        "last_position_at": v.last_position_at.isoformat() if v.last_position_at else None,
    } for v in vehicles if v.last_latitude is not None and v.last_longitude is not None]


@app.post("/vehicles/new")
def vehicles_new(request: Request, registration: str = Form(...), description: str = Form(""),
                  traccar_device_id: str = Form(""), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.save_vehicle(db, registration, description, traccar_device_id)
    return RedirectResponse("/vehicles", status_code=303)


@app.post("/vehicles/{vehicle_id}/deactivate")
def vehicles_deactivate(request: Request, vehicle_id: int, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.deactivate_vehicle(db, vehicle_id)
    return RedirectResponse("/vehicles", status_code=303)


@app.get("/vehicle-checks", response_class=HTMLResponse)
def vehicle_checks_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    import json
    checks = crud.list_vehicle_checks(db)
    checklist_labels = {key: label for group in crud.WALKAROUND_CHECKLIST for key, label in group["checks"]}
    for check in checks:
        check.parsed_items = json.loads(check.items_json)  # attach for template convenience
    return templates.TemplateResponse(request, "vehicle_checks.html", {
        "user": user, "active": "vehicle-checks", "checks": checks, "checklist_labels": checklist_labels,
    })


# --- clock points + timesheets (office admin) -----------------------------------------------

@app.get("/clock-points", response_class=HTMLResponse)
def clock_points_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "clock_points.html", {
        "user": user, "active": "clock-points", "clock_points": crud.list_clock_points(db),
        "drivers_status": crud.list_all_drivers_time_status(db),
    })


@app.post("/clock-points/new")
def clock_points_new(request: Request, name: str = Form(...), db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.create_clock_point(db, name)
    return RedirectResponse("/clock-points", status_code=303)


@app.post("/clock-points/{clock_point_id}/deactivate")
def clock_points_deactivate(request: Request, clock_point_id: int, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.deactivate_clock_point(db, clock_point_id)
    return RedirectResponse("/clock-points", status_code=303)


@app.get("/clock-points/{clock_point_id}/qr.png")
def clock_points_qr(request: Request, clock_point_id: int, db: Session = Depends(db_dependency)):
    """The printable QR code — points at /driver/clock/{token}. A driver's
    phone camera reads this straight into the browser, no app needed."""
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    point = db.get(models.ClockPoint, clock_point_id)
    if not point:
        raise HTTPException(status_code=404, detail="Clock point not found")
    import qrcode
    clock_url = str(request.base_url).rstrip("/") + f"/driver/clock/{point.token}"
    img = qrcode.make(clock_url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/timesheets", response_class=HTMLResponse)
def timesheets_page(request: Request, driver_id: str = "", date_from: str = "", date_to: str = "",
                     db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    today = date.today()
    date_from = date_from or (today - timedelta(days=7)).isoformat()
    date_to = date_to or today.isoformat()
    drivers = crud.list_drivers(db)
    entries, summary, tacho_records = [], {}, []
    selected_driver_id = int(driver_id) if driver_id else (drivers[0].user_id if drivers else None)
    if selected_driver_id:
        entries = crud.time_entries_for_driver(db, selected_driver_id, date_from, date_to)
        summary = crud.hours_summary(entries)
        tacho_records = crud.tachograph_records_for_driver(db, selected_driver_id, date_from, date_to)
    return templates.TemplateResponse(request, "timesheets.html", {
        "user": user, "active": "timesheets", "drivers": drivers, "selected_driver_id": selected_driver_id,
        "date_from": date_from, "date_to": date_to, "entries": entries, "summary": summary,
        "tacho_records": tacho_records, "vehicles": crud.list_vehicles(db),
        "tacho_total": sum((r.driving_hours for r in tacho_records), Decimal("0")),
    })


@app.post("/timesheets/tachograph")
def timesheets_save_tachograph(
    request: Request, driver_id: int = Form(...), record_date: str = Form(...),
    driving_hours: str = Form(...), vehicle_id: str = Form(""), notes: str = Form(""),
    source_reference: str = Form(""), db: Session = Depends(db_dependency),
):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.save_tachograph_record(
        db, driver_id, date.fromisoformat(record_date), Decimal(driving_hours), user.full_name,
        vehicle_id=int(vehicle_id) if vehicle_id else None, notes=notes, source_reference=source_reference,
    )
    return RedirectResponse(f"/timesheets?driver_id={driver_id}", status_code=303)


@app.post("/timesheets/tachograph/{record_id}/delete")
def timesheets_delete_tachograph(request: Request, record_id: int, driver_id: str = Form(""),
                                  db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.delete_tachograph_record(db, record_id)
    return RedirectResponse(f"/timesheets?driver_id={driver_id}" if driver_id else "/timesheets", status_code=303)


# --- office notifications (delivery completed toast) -----------------------------------------

@app.get("/api/notifications/deliveries-since")
def notifications_deliveries_since(request: Request, since: str, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid 'since' timestamp")
    deliveries = crud.deliveries_completed_since(db, since_dt)
    return [{
        "delivery_id": d.delivery_id, "order_number": d.order.order_number,
        "customer_name": d.order.customer.display_name, "driver_name": d.driver_name,
        "signed_at": d.pod_signed_at.isoformat(),
    } for d in deliveries]


# --- sales reports -----------------------------------------------------------------------

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, date_from: str = "", date_to: str = "", db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    today = date.today()
    date_from = date_from or (today - timedelta(days=7)).isoformat()
    date_to = date_to or today.isoformat()
    return templates.TemplateResponse(request, "reports.html", {
        "user": user, "active": "reports",
        "report": crud.sales_report(db, date_from, date_to),
    })


@app.get("/reports/export.csv")
def reports_export_csv(request: Request, date_from: str, date_to: str, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    import csv
    import io
    report = crud.sales_report(db, date_from, date_to)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Order number", "Date", "Customer", "Project", "Subtotal", "Tax", "Total"])
    for o in report["orders"]:
        writer.writerow([o.order_number, o.requested_date, o.customer.display_name, o.project,
                          f"{o.subtotal:.2f}", f"{o.tax_total:.2f}", f"{o.total:.2f}"])
    writer.writerow([])
    writer.writerow(["", "", "", "TOTAL", f"{report['subtotal']:.2f}", f"{report['tax_total']:.2f}", f"{report['total']:.2f}"])
    filename = f"Sales_Report_{date_from}_to_{date_to}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/reports/export.pdf")
def reports_export_pdf(request: Request, date_from: str, date_to: str, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = crud.sales_report(db, date_from, date_to)
    pdf_bytes = report_pdf.generate_sales_report_pdf(report, LOGO_PATH)
    filename = f"Sales_Report_{date_from}_to_{date_to}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


# --- Xero ------------------------------------------------------------------------------

@app.get("/xero", response_class=HTMLResponse)
def xero_page(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "xero.html", {
        "user": user, "active": "xero", "connection": crud.get_xero_connection(db),
        "configured": bool(XERO_CLIENT_ID and XERO_CLIENT_SECRET),
    })


@app.get("/xero/connect")
def xero_connect(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not (XERO_CLIENT_ID and XERO_CLIENT_SECRET):
        raise HTTPException(status_code=400, detail="XERO_CLIENT_ID / XERO_CLIENT_SECRET aren't set yet")
    state = signer.dumps({"user_id": user.user_id})
    url = xero_client.build_authorize_url(XERO_CLIENT_ID, XERO_REDIRECT_URI, state)
    return RedirectResponse(url, status_code=303)


@app.get("/xero/callback")
def xero_callback(request: Request, code: str = "", state: str = "", error: str = "",
                   db: Session = Depends(db_dependency)):
    if error:
        return RedirectResponse(f"/xero?error={error}", status_code=303)
    try:
        data = signer.loads(state)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state — please try connecting again")
    user = db.get(models.AppUser, data.get("user_id"))
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")

    tokens = xero_client.exchange_code_for_tokens(XERO_CLIENT_ID, XERO_CLIENT_SECRET, code, XERO_REDIRECT_URI)
    tenants = xero_client.get_connected_tenants(tokens["access_token"])
    tenant = tenants[0] if tenants else {"tenantId": "", "tenantName": ""}
    crud.save_xero_connection(
        db, tenant["tenantId"], tenant.get("tenantName", ""),
        xero_client.tokens_to_row_fields(tokens), user.full_name,
    )
    return RedirectResponse("/xero", status_code=303)


@app.post("/xero/disconnect")
def xero_disconnect(request: Request, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    crud.disconnect_xero(db)
    return RedirectResponse("/xero", status_code=303)



@app.get("/driver/login", response_class=HTMLResponse)
def driver_login_page(request: Request, next: str = "", db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if user:
        safe_next = next if next.startswith("/driver/") else "/driver"
        return RedirectResponse(safe_next if user.role == "Driver" else "/", status_code=303)
    return templates.TemplateResponse(request, "driver_login.html", {
        "user": None, "drivers": crud.list_drivers(db), "next": next,
    })


@app.post("/driver/login")
def driver_login_submit(request: Request, username: str = Form(...), pin: str = Form(...),
                         next: str = Form(""), db: Session = Depends(db_dependency)):
    user = crud.authenticate(db, username, pin)
    if not user or user.role != "Driver":
        return templates.TemplateResponse(request, "driver_login.html", {
            "user": None, "drivers": crud.list_drivers(db), "error": "Incorrect PIN — try again", "next": next,
        }, status_code=401)
    token = signer.dumps({"user_id": user.user_id})
    # Only ever redirect within our own /driver/... routes — never follow an
    # arbitrary external "next" value from a URL.
    destination = next if next.startswith("/driver/") else "/driver"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.get("/driver", response_class=HTMLResponse)
def driver_dashboard(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    clock_points = crud.list_clock_points(db)
    return templates.TemplateResponse(request, "driver_dashboard.html", {
        "user": user, "jobs": crud.deliveries_for_driver(db, user.user_id),
        "checked_in_today": crud.driver_has_checked_in_today(db, user.user_id),
        "active_entry": crud.get_active_time_entry(db, user.user_id),
        "first_clock_point": clock_points[0] if clock_points else None,
    })


# --- driver hours: clock in/out via QR scan --------------------------------------------

@app.get("/driver/clock/{token}", response_class=HTMLResponse)
def driver_clock_page(token: str, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        # Bounce to login, then straight back to this exact QR link once signed in.
        return RedirectResponse(f"/driver/login?next=/driver/clock/{token}", status_code=303)
    point = crud.get_clock_point_by_token(db, token)
    if not point:
        raise HTTPException(status_code=404, detail="This clock-in code isn't recognised — check with the office.")
    return templates.TemplateResponse(request, "driver_clock.html", {
        "user": user, "clock_point": point, "vehicles": crud.list_vehicles(db),
        "active_entry": crud.get_active_time_entry(db, user.user_id),
    })


@app.post("/driver/clock/{token}/start")
def driver_clock_start(
    token: str, request: Request, activity_type: str = Form(...), vehicle_id: str = Form(""),
    db: Session = Depends(db_dependency),
):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    point = crud.get_clock_point_by_token(db, token)
    if not point:
        raise HTTPException(status_code=404, detail="Clock point not found")
    crud.start_activity(
        db, user.user_id, activity_type, clock_point_id=point.clock_point_id,
        vehicle_id=int(vehicle_id) if vehicle_id else None, source="qr_scan",
    )
    return RedirectResponse("/driver", status_code=303)


@app.post("/driver/clock/{token}/out")
def driver_clock_out(token: str, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    crud.clock_out(db, user.user_id)
    return RedirectResponse("/driver", status_code=303)


# --- driver hours: cab QR scan (per-vehicle, handles driver handover) -------------------

@app.get("/driver/clock/vehicle/{token}", response_class=HTMLResponse)
def driver_vehicle_clock_page(token: str, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse(f"/driver/login?next=/driver/clock/vehicle/{token}", status_code=303)
    vehicle = crud.get_vehicle_by_qr_token(db, token)
    if not vehicle:
        raise HTTPException(status_code=404, detail="This vehicle's QR code isn't recognised — check with the office.")
    current_driver_entry = crud.get_active_driver_for_vehicle(db, vehicle.vehicle_id)
    return templates.TemplateResponse(request, "driver_vehicle_clock.html", {
        "user": user, "vehicle": vehicle, "current_driver_entry": current_driver_entry,
        "is_already_driving_this": bool(current_driver_entry and current_driver_entry.driver_user_id == user.user_id),
    })


@app.post("/driver/clock/vehicle/{token}/start")
def driver_vehicle_clock_start(token: str, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    vehicle = crud.get_vehicle_by_qr_token(db, token)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    crud.start_driving_vehicle(db, user.user_id, vehicle.vehicle_id)
    return RedirectResponse("/driver", status_code=303)


@app.post("/driver/clock/vehicle/{token}/other")
def driver_vehicle_clock_other(token: str, request: Request, activity_type: str = Form(...),
                                db: Session = Depends(db_dependency)):
    """From the cab-QR page's secondary options — Yard Work / Break / Other
    instead of driving (e.g. scanned by mistake, or doing something else
    near the vehicle)."""
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    crud.start_activity(db, user.user_id, activity_type)
    return RedirectResponse("/driver", status_code=303)


# --- driver vehicle check (daily walkaround, DVSA-style) ---------------------------------

@app.get("/driver/vehicle-check", response_class=HTMLResponse)
def driver_vehicle_check_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    return templates.TemplateResponse(request, "driver_vehicle_check.html", {
        "user": user, "vehicles": crud.list_vehicles(db), "checklist": crud.WALKAROUND_CHECKLIST,
    })


@app.post("/driver/vehicle-check/submit")
async def driver_vehicle_check_submit(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/driver/login", status_code=303)
    form = await request.form()
    vehicle_id = int(form["vehicle_id"])
    all_keys = [key for group in crud.WALKAROUND_CHECKLIST for key, _ in group["checks"]]
    items = {key: form.get(key, "defect") for key in all_keys}  # unticked = treated as needing attention

    signature = form.get("signature")
    sig_name = ""
    if signature is not None and hasattr(signature, "file"):
        sig_name = f"{uuid.uuid4().hex}.png"
        (UPLOAD_DIR / sig_name).write_bytes(signature.file.read())

    crud.save_vehicle_check(
        db, user.user_id, vehicle_id, items,
        defect_notes=form.get("defect_notes", ""), signed_by=user.full_name, signature_path=sig_name,
    )
    return RedirectResponse("/driver", status_code=303)


# --- driver-facing page: no login, just the link (POD + tracking) ------------------------

@app.get("/d/{token}", response_class=HTMLResponse)
def driver_page(token: str, request: Request, db: Session = Depends(db_dependency)):
    delivery = crud.get_delivery_by_token(db, token)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery link not found")
    return templates.TemplateResponse(request, "delivery.html", {
        "delivery": delivery, "order": delivery.order, "token": token,
    })


@app.post("/api/deliveries/{token}/ping")
def ping(token: str, latitude: float = Form(...), longitude: float = Form(...),
         db: Session = Depends(db_dependency)):
    delivery = crud.get_delivery_by_token(db, token)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery link not found")
    crud.record_ping(db, delivery, latitude, longitude)
    return {"ok": True}


@app.post("/api/deliveries/{token}/pod")
def submit_pod(
    token: str, signed_by: str = Form(...),
    latitude: Optional[float] = Form(None), longitude: Optional[float] = Form(None),
    signature: UploadFile = File(...), photo: Optional[UploadFile] = File(None),
    db: Session = Depends(db_dependency),
):
    delivery = crud.get_delivery_by_token(db, token)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery link not found")

    sig_name = f"{uuid.uuid4().hex}.png"
    (UPLOAD_DIR / sig_name).write_bytes(signature.file.read())
    photo_name = ""
    if photo is not None and photo.filename:
        photo_name = f"{uuid.uuid4().hex}_{photo.filename}"
        (UPLOAD_DIR / photo_name).write_bytes(photo.file.read())

    crud.record_pod(db, delivery, signed_by, sig_name, photo_name, latitude, longitude)
    return {"ok": True}


@app.get("/d/{token}/pod.pdf")
def download_pod_by_token(token: str, db: Session = Depends(db_dependency)):
    """No login needed — same principle as the delivery link itself: whoever
    has the link can view/download that one delivery's signed POD."""
    delivery = crud.get_delivery_by_token(db, token)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery link not found")
    if delivery.status != "Delivered":
        raise HTTPException(status_code=409, detail="This delivery hasn't been signed off yet")
    pdf_bytes = pod_pdf.generate_pod_pdf(delivery, UPLOAD_DIR, LOGO_PATH)
    filename = f"POD_{delivery.order.order_number.replace(' ', '_')}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


@app.get("/api/deliveries/{delivery_id}/pod.pdf")
def download_pod_office(request: Request, delivery_id: int, db: Session = Depends(db_dependency)):
    user = require_office_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    delivery = crud.get_delivery(db, delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if delivery.status != "Delivered":
        raise HTTPException(status_code=409, detail="This delivery hasn't been signed off yet")
    pdf_bytes = pod_pdf.generate_pod_pdf(delivery, UPLOAD_DIR, LOGO_PATH)
    filename = f"POD_{delivery.order.order_number.replace(' ', '_')}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})
