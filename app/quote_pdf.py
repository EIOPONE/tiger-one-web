"""Quote / order confirmation → PDF. Replaces pdf_engine.py + quote_document.py
for the actual PDF generation — those relied on a locally-installed browser
(Edge/Chrome) which only exists on the office PC, not on Render's Linux
servers. This uses reportlab's platypus layer (variable-length item table,
same approach as report_pdf.py) so it works identically in both places.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#17191C")
MUTED = colors.HexColor("#70757C")
ORANGE = colors.HexColor("#E3783E")
LINE = colors.HexColor("#DFE2E4")
SURFACE = colors.HexColor("#F4F5F5")


def _cash(value) -> str:
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return "£0.00"


def _number(value) -> str:
    try:
        return f"{float(value):,.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "0"


def _uk_date(value) -> str:
    from datetime import datetime
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text[:19], fmt)
            return f"{parsed.day} {parsed.strftime('%B %Y')}"
        except (ValueError, TypeError):
            pass
    return text or "—"


def generate_quote_or_order_pdf(payload: dict, logo_path: Path | None, is_order: bool = False) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=16 * mm, bottomMargin=16 * mm, leftMargin=16 * mm, rightMargin=16 * mm,
        title="Order Confirmation" if is_order else "Quotation",
    )
    styles = getSampleStyleSheet()
    doc_label = "ORDER CONFIRMATION" if is_order else "QUOTATION"
    doc_number = payload.get("order_number") if is_order else payload.get("quote_number")

    title_style = ParagraphStyle("DocTitle", parent=styles["Title"], textColor=colors.white, fontSize=15,
                                  fontName="Helvetica-Bold", alignment=1)
    meta_style = ParagraphStyle("DocMeta", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, alignment=1)
    company_style = ParagraphStyle("Company", parent=styles["Normal"], textColor=MUTED, fontSize=8, leading=12)
    box_title_style = ParagraphStyle("BoxTitle", parent=styles["Normal"], textColor=INK, fontSize=8, fontName="Helvetica-Bold")
    field_label_style = ParagraphStyle("FieldLabel", parent=styles["Normal"], textColor=MUTED, fontSize=8, fontName="Helvetica-Bold")
    field_value_style = ParagraphStyle("FieldValue", parent=styles["Normal"], textColor=INK, fontSize=9)
    header_style = ParagraphStyle("TableHeader", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, fontName="Helvetica-Bold")
    cell_style = ParagraphStyle("TableCell", parent=styles["Normal"], textColor=INK, fontSize=8.5)
    right_style = ParagraphStyle("TableCellRight", parent=cell_style, alignment=2)
    notes_style = ParagraphStyle("Notes", parent=styles["Normal"], textColor=INK, fontSize=8.5, leading=12)

    story = []

    # --- header: logo + company details, and a dark reference box -----------------
    logo_cell = Image(str(logo_path), width=42 * mm, height=18 * mm, kind="proportional") if logo_path and logo_path.exists() else ""
    company_lines = [
        "Fresh concrete mixed on site", payload.get("company_address", ""),
        payload.get("company_telephone", ""), payload.get("company_email", ""),
    ]
    company_cell = Paragraph("<br/>".join(l for l in company_lines if l), company_style)

    meta_rows = [
        [Paragraph(doc_label, title_style)],
        [Paragraph(f"Reference: <b>{doc_number}</b>", meta_style)],
        [Paragraph(f"Revision: <b>{payload.get('revision', 'A')}</b>", meta_style)],
        [Paragraph(f"Date: <b>{_uk_date(payload.get('created_at'))}</b>", meta_style)],
    ]
    if not is_order:
        meta_rows.append([Paragraph(f"Valid for: <b>{payload.get('validity_days', 14)} days</b>", meta_style)])
    meta_box = Table(meta_rows, colWidths=[62 * mm])
    meta_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INK),
        ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 2), ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    header_table = Table([[logo_cell, company_cell, meta_box]], colWidths=[46 * mm, 82 * mm, 62 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, ORANGE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6 * mm))

    # --- customer + pour requirements boxes ----------------------------------------
    def box(title, fields):
        rows = [[Paragraph(title, box_title_style)]]
        for label, value in fields:
            rows.append([Table([[Paragraph(label, field_label_style), Paragraph(str(value or "—"), field_value_style)]],
                                colWidths=[26 * mm, 60 * mm])])
        t = Table(rows, colWidths=[90 * mm])
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, LINE),
            ("LINEBELOW", (0, 0), (0, 0), 0.5, LINE),
            ("BACKGROUND", (0, 0), (0, 0), SURFACE),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    address = ", ".join(filter(None, [payload.get("address_1"), payload.get("address_2"), payload.get("town"), payload.get("postcode")]))
    customer_box = box("Customer", [
        ("Type", payload.get("customer_type")), ("Name", payload.get("customer_name")),
        ("Contact", payload.get("contact_name")), ("Telephone", payload.get("telephone") or payload.get("mobile")),
        ("Address", address),
    ])
    pour_box = box("Pour requirements", [
        ("Project", payload.get("project")), ("Site", payload.get("site_address")),
        ("Requested", _uk_date(payload.get("requested_date"))), ("Status", payload.get("status")),
        ("Payment", payload.get("payment_terms") or "To be agreed"),
    ])
    boxes = Table([[customer_box, pour_box]], colWidths=[92 * mm, 92 * mm])
    boxes.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(boxes)
    story.append(Spacer(1, 6 * mm))

    project_title_style = ParagraphStyle("ProjectTitle", parent=styles["Normal"], textColor=INK, fontSize=10.5, fontName="Helvetica-Bold")
    story.append(Paragraph(payload.get("project") or "Concrete supply quotation", project_title_style))
    story.append(Spacer(1, 3 * mm))

    # --- line items -----------------------------------------------------------------
    rows = [[
        Paragraph("Item", header_style), Paragraph("Description", header_style),
        Paragraph("Qty", header_style), Paragraph("Unit", header_style),
        Paragraph("Unit Price", header_style), Paragraph("Line Total", header_style),
    ]]
    for index, item in enumerate(payload.get("items") or [], start=1):
        rows.append([
            Paragraph(str(index), cell_style), Paragraph(str(item.get("description", "")), cell_style),
            Paragraph(_number(item.get("quantity")), right_style), Paragraph(str(item.get("unit", "m³")), cell_style),
            Paragraph(_cash(item.get("unit_price")), right_style), Paragraph(f"<b>{_cash(item.get('line_total'))}</b>", right_style),
        ])
    items_table = Table(rows, colWidths=[12 * mm, 68 * mm, 20 * mm, 18 * mm, 28 * mm, 28 * mm], repeatRows=1)
    item_style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            item_style.append(("BACKGROUND", (0, i), (-1, i), SURFACE))
    items_table.setStyle(TableStyle(item_style))
    story.append(items_table)
    story.append(Spacer(1, 5 * mm))

    # --- totals ---------------------------------------------------------------------
    totals_rows = [
        ["Subtotal", _cash(payload.get("subtotal"))],
        [f"VAT at {_number(payload.get('tax_rate'))}%", _cash(payload.get("tax_total"))],
        [("Order total" if is_order else "Quotation total"), _cash(payload.get("total"))],
    ]
    totals_table = Table(totals_rows, colWidths=[45 * mm, 30 * mm], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 1), 0.5, LINE),
        ("BACKGROUND", (0, 2), (0, 2), ORANGE), ("TEXTCOLOR", (0, 2), (0, 2), colors.white),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"), ("BOX", (1, 2), (1, 2), 1, ORANGE),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(totals_table)
    story.append(Spacer(1, 6 * mm))

    # --- terms + notes ----------------------------------------------------------------
    terms_text = (
        "Final concrete quantity is charged as delivered. The customer must provide safe and suitable "
        "site access, a suitable washout area and accurate dimensions. Cancellation, waiting-time and "
        "additional pumping charges may apply."
    )
    notes_text = payload.get("commercial_notes") or "No additional notes."
    terms_table = Table([[Paragraph("Commercial terms", box_title_style)], [Paragraph(terms_text, notes_style)]], colWidths=[90 * mm])
    notes_table = Table([[Paragraph("Notes", box_title_style)], [Paragraph(notes_text, notes_style)]], colWidths=[90 * mm])
    for t in (terms_table, notes_table):
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (0, 0), SURFACE),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
    story.append(KeepTogether(Table([[terms_table, notes_table]], colWidths=[92 * mm, 92 * mm],
                                     style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))))
    story.append(Spacer(1, 8 * mm))

    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], textColor=MUTED, fontSize=7)
    story.append(Paragraph(
        f"Tiger Concrete Ltd &nbsp;·&nbsp; Generated by Tiger One &nbsp;·&nbsp; Powered by ONE, built by EIOP Software "
        f"&nbsp;·&nbsp; {doc_label} {doc_number} &middot; REV {payload.get('revision', 'A')}",
        footer_style,
    ))

    doc.build(story)
    return buf.getvalue()
