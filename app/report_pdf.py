"""Sales report → PDF. Uses reportlab's platypus layer (not raw canvas like
pod_pdf.py) because a report table is variable-length and needs proper
page-break handling once there are enough orders to span multiple pages."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#17191C")
MUTED = colors.HexColor("#70757C")
ORANGE = colors.HexColor("#E3783E")
LINE = colors.HexColor("#DFE2E4")
SURFACE = colors.HexColor("#F4F5F5")


def generate_sales_report_pdf(report: dict, logo_path: Path | None = None) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title="Sales Report",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TigerTitle", parent=styles["Title"], textColor=INK, fontSize=20, spaceAfter=2)
    subtitle_style = ParagraphStyle("TigerSubtitle", parent=styles["Normal"], textColor=MUTED, fontSize=10)
    kpi_label_style = ParagraphStyle("KpiLabel", parent=styles["Normal"], textColor=MUTED, fontSize=8, alignment=1)
    kpi_value_style = ParagraphStyle("KpiValue", parent=styles["Normal"], textColor=INK, fontSize=15, alignment=1, fontName="Helvetica-Bold")

    story = []

    if logo_path and logo_path.exists():
        try:
            story.append(Image(str(logo_path), width=32 * mm, height=16 * mm, kind="proportional"))
            story.append(Spacer(1, 6 * mm))
        except Exception:
            pass

    story.append(Paragraph("Sales Report", title_style))
    story.append(Paragraph(f"{report['date_from']} to {report['date_to']} — completed (delivered) orders", subtitle_style))
    story.append(Spacer(1, 8 * mm))

    kpis = [
        ("Orders delivered", str(report["order_count"])),
        ("Sales (excl. tax)", f"£{report['subtotal']:.2f}"),
        ("Tax", f"£{report['tax_total']:.2f}"),
        ("Total (inc. tax)", f"£{report['total']:.2f}"),
    ]
    kpi_table = Table(
        [[Paragraph(v, kpi_value_style) for _, v in kpis], [Paragraph(l, kpi_label_style) for l, _ in kpis]],
        colWidths=[42 * mm] * 4,
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 10 * mm))

    header_style = ParagraphStyle("TableHeader", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, fontName="Helvetica-Bold")
    cell_style = ParagraphStyle("TableCell", parent=styles["Normal"], textColor=INK, fontSize=8.5)
    right_style = ParagraphStyle("TableCellRight", parent=cell_style, alignment=2)

    rows = [[
        Paragraph("Order", header_style), Paragraph("Date", header_style),
        Paragraph("Customer", header_style), Paragraph("Project", header_style),
        Paragraph("Subtotal", header_style), Paragraph("Tax", header_style), Paragraph("Total", header_style),
    ]]
    for o in report["orders"]:
        rows.append([
            Paragraph(o.order_number, cell_style), Paragraph(o.requested_date or "—", cell_style),
            Paragraph(o.customer.display_name, cell_style), Paragraph(o.project or "—", cell_style),
            Paragraph(f"£{o.subtotal:.2f}", right_style), Paragraph(f"£{o.tax_total:.2f}", right_style),
            Paragraph(f"£{o.total:.2f}", right_style),
        ])
    rows.append([
        "", "", "", Paragraph("<b>TOTAL</b>", cell_style),
        Paragraph(f"<b>£{report['subtotal']:.2f}</b>", right_style),
        Paragraph(f"<b>£{report['tax_total']:.2f}</b>", right_style),
        Paragraph(f"<b>£{report['total']:.2f}</b>", right_style),
    ])

    if report["orders"]:
        col_widths = [24 * mm, 20 * mm, 38 * mm, 38 * mm, 22 * mm, 20 * mm, 22 * mm]
        table = Table(rows, colWidths=col_widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), INK),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, INK),
            ("LINEBELOW", (0, -1), (-1, -1), 1, INK),
            ("LINEABOVE", (0, -1), (-1, -1), 0.75, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
        for i in range(1, len(rows) - 1):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), SURFACE))
        table.setStyle(TableStyle(style))
        story.append(table)
    else:
        story.append(Paragraph("No completed orders in this date range.", subtitle_style))

    story.append(Spacer(1, 10 * mm))
    generated_style = ParagraphStyle("Generated", parent=styles["Normal"], textColor=MUTED, fontSize=7.5)
    story.append(Paragraph(
        f"Generated by Tiger One on {datetime.now().strftime('%d %B %Y, %H:%M')}", generated_style,
    ))

    doc.build(story)
    return buf.getvalue()
