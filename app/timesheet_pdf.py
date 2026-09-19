"""Timesheet → PDF. Same reportlab platypus approach as report_pdf.py and
quote_pdf.py — pure Python, works identically on Render and the office PC.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import tz

INK = colors.HexColor("#17191C")
MUTED = colors.HexColor("#70757C")
ORANGE = colors.HexColor("#E3783E")
LINE = colors.HexColor("#DFE2E4")
SURFACE = colors.HexColor("#F4F5F5")
HOLIDAY_BG = colors.HexColor("#FFF0E7")


def generate_timesheet_pdf(driver_name: str, date_from: str, date_to: str, days: list[dict],
                            totals: dict, logo_path: Path | None = None) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Timesheet — {driver_name}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Title"], textColor=INK, fontSize=18, spaceAfter=2)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], textColor=MUTED, fontSize=10)
    header_style = ParagraphStyle("Header", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, fontName="Helvetica-Bold")
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], textColor=INK, fontSize=9)
    right_style = ParagraphStyle("CellRight", parent=cell_style, alignment=2)
    holiday_style = ParagraphStyle("Holiday", parent=cell_style, textColor=ORANGE, fontName="Helvetica-Bold")

    story = []
    if logo_path and logo_path.exists():
        try:
            story.append(Image(str(logo_path), width=32 * mm, height=16 * mm, kind="proportional"))
            story.append(Spacer(1, 6 * mm))
        except Exception:
            pass

    story.append(Paragraph(f"Timesheet — {driver_name}", title_style))
    story.append(Paragraph(f"{date_from} to {date_to}", subtitle_style))
    story.append(Spacer(1, 8 * mm))

    rows = [[
        Paragraph("Date", header_style), Paragraph("Clock in", header_style),
        Paragraph("Clock out", header_style), Paragraph("Hours worked", header_style),
        Paragraph("Driving hours", header_style),
    ]]
    row_styles = []
    for i, day in enumerate(days, start=1):
        day_label = day["date"].strftime("%a %d %b")
        if day["is_holiday"]:
            rows.append([
                Paragraph(day_label, cell_style), Paragraph("HOLIDAY", holiday_style),
                Paragraph("—", cell_style), Paragraph("0.00", right_style), Paragraph("0.00", right_style),
            ])
            row_styles.append(("BACKGROUND", (0, i), (-1, i), HOLIDAY_BG))
        elif day["worked"]:
            rows.append([
                Paragraph(day_label, cell_style),
                Paragraph(tz.uk_time_str(day["clock_in"]) if day["clock_in"] else "—", cell_style),
                Paragraph(tz.uk_time_str(day["clock_out"]) if day["clock_out"] else "still clocked in", cell_style),
                Paragraph(f"{day['hours_worked']:.2f}", right_style),
                Paragraph(f"{day['driving_hours']:.2f}", right_style),
            ])
        else:
            rows.append([
                Paragraph(day_label, cell_style), Paragraph("—", cell_style), Paragraph("—", cell_style),
                Paragraph("—", right_style), Paragraph("—", right_style),
            ])
            row_styles.append(("TEXTCOLOR", (0, i), (-1, i), MUTED))

    # Totals row — the last row of the table, right after the last day.
    rows.append([
        "", "", Paragraph("<b>TOTAL</b>", cell_style),
        Paragraph(f"<b>{totals['total_hours_worked']:.2f}</b>", right_style),
        Paragraph(f"<b>{totals['total_driving_hours']:.2f}</b>", right_style),
    ])

    table = Table(rows, colWidths=[32 * mm, 30 * mm, 32 * mm, 32 * mm, 32 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 1, INK),
        ("BACKGROUND", (0, -1), (-1, -1), SURFACE),
    ] + row_styles
    table.setStyle(TableStyle(style))
    story.append(table)

    if totals.get("holiday_days"):
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(
            f"Includes {totals['holiday_days']} holiday day(s), shown at zero hours — "
            f"holiday time is tracked separately and does not count toward Working Time Directive totals.",
            subtitle_style,
        ))

    story.append(Spacer(1, 10 * mm))
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], textColor=MUTED, fontSize=7)
    from datetime import datetime
    story.append(Paragraph(f"Generated by Tiger One on {datetime.now().strftime('%d %B %Y, %H:%M')}", footer_style))

    doc.build(story)
    return buf.getvalue()
