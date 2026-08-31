from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import crud, models
from . import pdf_engine, quote_document
from .database import get_session, init_db

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR = Path(os.environ.get("PDF_DIR", BASE_DIR.parent / "generated_pdfs"))
PDF_DIR.mkdir(parents=True, exist_ok=True)
LOGO_PATH = BASE_DIR / "branding" / "tiger_concrete_logo.jpg"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
signer = URLSafeSerializer(SECRET_KEY, salt="tiger-one-session")

app = FastAPI(title="Tiger One")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    init_db()
    with get_session() as db:
        crud.ensure_admin_user(db)


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
    delivery = crud.create_delivery(db, order_id, driver_name, vehicle)
    return {"delivery_id": delivery.delivery_id, "driver_link": f"/d/{delivery.access_token}"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(db_dependency), user=Depends(current_user)):
    return crud.database_summary(db)


# --- Web UI (office screens) ------------------------------------------------------------

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
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/web-logout")
def web_logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "active": "dashboard", "summary": crud.database_summary(db),
        "jobs_today": crud.todays_jobs(db, today),
        "today_label": datetime.now().strftime("%A %d %B %Y"),
    })


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    return templates.TemplateResponse(request, "customers.html", {
        "user": user, "active": "customers", "customers": customers,
    })


@app.post("/customers/new")
def customers_new(
    request: Request, customer_type: str = Form(...), display_name: str = Form(...),
    contact_name: str = Form(""), mobile: str = Form(""), email: str = Form(""),
    payment_terms: str = Form(""), address_1: str = Form(""), db: Session = Depends(db_dependency),
):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.save_customer(db, {
        "customer_type": customer_type, "display_name": display_name, "contact_name": contact_name,
        "mobile": mobile, "email": email, "payment_terms": payment_terms, "address_1": address_1,
    })
    return RedirectResponse("/customers", status_code=303)


@app.get("/materials", response_class=HTMLResponse)
def materials_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "materials.html", {
        "user": user, "active": "materials", "materials": crud.material_balances(db),
    })


@app.post("/materials/new")
def materials_new(
    request: Request, code: str = Form(...), name: str = Form(...), unit: str = Form(...),
    reorder_level: float = Form(0), reorder_quantity: float = Form(0), unit_cost: float = Form(0),
    supplier: str = Form(""), db: Session = Depends(db_dependency),
):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.save_material(db, {
        "code": code, "name": name, "unit": unit, "on_hand": 0, "reorder_level": reorder_level,
        "reorder_quantity": reorder_quantity, "unit_cost": unit_cost, "supplier": supplier,
    })
    return RedirectResponse("/materials", status_code=303)


@app.post("/materials/{material_id}/receive")
def materials_receive(request: Request, material_id: int, quantity: float = Form(...), db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.receive_stock(db, material_id, quantity)
    return RedirectResponse("/materials", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    products = db.query(models.Product).filter(models.Product.active.is_(True)).order_by(models.Product.name).all()
    return templates.TemplateResponse(request, "products.html", {
        "user": user, "active": "products", "products": products, "materials_json": materials_json(db),
    })


@app.post("/products/new")
async def products_new(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    quotes = db.query(models.Quote).order_by(models.Quote.created_at.desc()).all()
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    return templates.TemplateResponse(request, "quotes.html", {
        "user": user, "active": "quotes", "quotes": quotes, "customers": customers,
        "products_json": products_json(db),
    })


@app.post("/quotes/new")
async def quotes_new(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.set_quote_status(db, quote_id, status)
    return RedirectResponse("/quotes", status_code=303)


@app.post("/quotes/{quote_id}/allocate")
def quotes_allocate(request: Request, quote_id: int, allocate: str = Form(...), db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.set_quote_allocate_stock(db, quote_id, allocate == "true")
    return RedirectResponse("/quotes", status_code=303)


@app.get("/quotes/{quote_id}/pdf")
def quote_pdf(quote_id: int, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    payload = crud.quote_payload(db, quote_id)
    html_path = PDF_DIR / f"quote_{quote_id}.html"
    pdf_path = PDF_DIR / f"Quotation_{payload['quote_number'].replace(' ', '_')}_Rev_{payload['revision']}.pdf"
    quote_document.write_quote_html(html_path, payload, LOGO_PATH)
    ok, result = pdf_engine.print_to_pdf(html_path, pdf_path)
    html_path.unlink(missing_ok=True)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Could not create the PDF: {result}")
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    orders = db.query(models.Order).order_by(models.Order.created_at.desc()).all()
    customers = db.query(models.Customer).filter(models.Customer.active.is_(True)).order_by(models.Customer.display_name).all()
    return templates.TemplateResponse(request, "orders.html", {
        "user": user, "active": "orders", "orders": orders, "customers": customers,
        "products_json": products_json(db),
    })


@app.post("/orders/new")
async def orders_new(request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.set_order_status(db, order_id, status)
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/allocate")
def orders_allocate(request: Request, order_id: int, allocate: str = Form(...), db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.set_order_allocate_stock(db, order_id, allocate == "true")
    return RedirectResponse("/orders", status_code=303)


@app.get("/orders/{order_id}/pdf")
def order_pdf(order_id: int, request: Request, db: Session = Depends(db_dependency)):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    payload = crud.order_payload(db, order_id)
    html_path = PDF_DIR / f"order_{order_id}.html"
    pdf_path = PDF_DIR / f"Order_Confirmation_{payload['order_number'].replace(' ', '_')}.pdf"
    quote_document.write_order_html(html_path, payload, LOGO_PATH)
    ok, result = pdf_engine.print_to_pdf(html_path, pdf_path)
    html_path.unlink(missing_ok=True)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Could not create the PDF: {result}")
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


@app.post("/orders/{order_id}/deliveries")
def orders_schedule_delivery_page(
    request: Request, order_id: int, driver_name: str = Form(...), vehicle: str = Form(...),
    db: Session = Depends(db_dependency),
):
    user = get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    crud.create_delivery(db, order_id, driver_name, vehicle)
    return RedirectResponse("/orders", status_code=303)


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
