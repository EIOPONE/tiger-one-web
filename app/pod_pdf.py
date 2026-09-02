"""Signed POD → PDF.

Deliberately separate from pdf_engine.py (which prints quotes/orders via a
local headless browser and only works on Windows/PC). This one uses
reportlab — pure Python, no external binary — so it works the same on the
office PC and on Render.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from . import models

INK = (0x17 / 255, 0x19 / 255, 0x1C / 255)
MUTED = (0x70 / 255, 0x75 / 255, 0x7C / 255)
ORANGE = (0xE3 / 255, 0x78 / 255, 0x3E / 255)


def _generate_location_map(latitude, longitude, width_px: int = 360, height_px: int = 200):
    """A small static map image with a pin at the sign-off location, using
    OpenStreetMap tiles — no API key needed. Returns a BytesIO PNG, or None
    if the map couldn't be generated (no internet reachable, tile server
    down, etc) — the caller falls back to plain text coordinates in that
    case, so a map failure never breaks the PDF itself."""
    try:
        from staticmap import CircleMarker, StaticMap
        m = StaticMap(width_px, height_px)
        m.add_marker(CircleMarker((float(longitude), float(latitude)), "#E3783E", 14))
        image = m.render(zoom=15)
        buf = BytesIO()
        image.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


def generate_pod_pdf(delivery: models.Delivery, upload_dir: Path, logo_path: Path | None = None) -> bytes:
    order = delivery.order
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin

    if logo_path and logo_path.exists():
        try:
            c.drawImage(ImageReader(str(logo_path)), margin, y - 16 * mm, height=16 * mm,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(*INK)
    c.drawRightString(width - margin, y - 6 * mm, "Proof of Delivery")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(*MUTED)
    c.drawRightString(width - margin, y - 12 * mm, f"Order {order.order_number}")
    y -= 26 * mm

    c.setStrokeColorRGB(0.87, 0.89, 0.9)
    c.line(margin, y, width - margin, y)
    y -= 10 * mm

    def field(label, value):
        nonlocal y
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(*MUTED)
        c.drawString(margin, y, label.upper())
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(*INK)
        c.drawString(margin, y - 5 * mm, str(value) if value else "—")
        y -= 12 * mm

    field("Customer", order.customer.display_name)
    field("Project", order.project)
    field("Site address", order.site_address)
    field("Driver / vehicle", f"{delivery.driver_name} · {delivery.vehicle}".strip(" ·"))
    field("Signed by", delivery.pod_signed_by)
    field("Signed at", delivery.pod_signed_at.strftime("%d %B %Y, %H:%M UTC") if delivery.pod_signed_at else "—")

    if delivery.pod_latitude and delivery.pod_longitude:
        map_buf = _generate_location_map(delivery.pod_latitude, delivery.pod_longitude)
        if map_buf:
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(*MUTED)
            c.drawString(margin, y, "LOCATION AT SIGN-OFF")
            y -= 3 * mm
            map_h = 32 * mm
            map_w = 58 * mm
            c.drawImage(ImageReader(map_buf), margin, y - map_h, width=map_w, height=map_h)
            c.setStrokeColorRGB(0.87, 0.89, 0.9)
            c.rect(margin, y - map_h, map_w, map_h)
            y -= map_h + 10 * mm
        else:
            field("GPS location at sign-off", f"{delivery.pod_latitude}, {delivery.pod_longitude}")

    y -= 4 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(*MUTED)
    c.drawString(margin, y, "SIGNATURE")
    y -= 3 * mm
    sig_box_h = 30 * mm
    c.setStrokeColorRGB(0.87, 0.89, 0.9)
    c.rect(margin, y - sig_box_h, 80 * mm, sig_box_h)
    if delivery.pod_signature_path:
        sig_file = upload_dir / delivery.pod_signature_path
        if sig_file.exists():
            try:
                c.drawImage(ImageReader(str(sig_file)), margin + 2 * mm, y - sig_box_h + 2 * mm,
                            width=76 * mm, height=sig_box_h - 4 * mm, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
    y -= sig_box_h + 10 * mm

    if delivery.pod_photo_path:
        photo_file = upload_dir / delivery.pod_photo_path
        if photo_file.exists():
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(*MUTED)
            c.drawString(margin, y, "SITE PHOTO")
            y -= 3 * mm
            photo_h = 60 * mm
            try:
                c.drawImage(ImageReader(str(photo_file)), margin, y - photo_h, width=100 * mm,
                            height=photo_h, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
            y -= photo_h + 6 * mm

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(*MUTED)
    c.drawString(margin, margin, "Generated by Tiger One")

    c.showPage()
    c.save()
    return buf.getvalue()
