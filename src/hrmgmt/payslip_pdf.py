"""The payslip PDF, drawn from the numbers stored when the run was approved. Nothing is
recalculated here, so a payslip always shows exactly what the CEO approved."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_NOT_SET = "Company details not set"


def _rs(value: Any) -> str:
    amount = Decimal(str(value))
    return f"{amount:,.2f}"


def render_payslip(
    *,
    company: dict[str, str],
    employee: dict[str, Any],
    month: date,
    figures: dict[str, Any],
) -> bytes:
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Payslip {month:%B %Y} {employee['code']}",
        author=company.get("name") or "Verigence HR",
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle(
        "small",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#475467"),
    )
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9, leading=12)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading3"], fontSize=10, spaceBefore=8, spaceAfter=3)

    story: list[Any] = [Paragraph(company.get("name") or _NOT_SET, h1)]
    for line in (
        company.get("address"),
        " · ".join(
            x
            for x in (
                f"PAN {company['pan']}" if company.get("pan") else "",
                f"PF {company['pf_registration']}" if company.get("pf_registration") else "",
                f"ESI {company['esi_registration']}" if company.get("esi_registration") else "",
            )
            if x
        ),
    ):
        if line:
            story.append(Paragraph(line, small))
    story += [Spacer(1, 6), Paragraph(f"<b>Payslip for {month:%B %Y}</b>", styles["Heading2"])]

    d = figures["days"]
    info = [
        [
            "Employee",
            f"{employee['name']} ({employee['code']})",
            "Designation",
            employee.get("designation") or "—",
        ],
        ["PAN", employee.get("pan_masked") or "—", "Days in month", str(d["inMonth"])],
        ["Loss of pay days", d["lopDays"], "Days paid", d["paidDays"]],
    ]
    t = Table(info, colWidths=[28 * mm, 62 * mm, 32 * mm, 52 * mm])
    t.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#475467")),
                ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#475467")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#d0d5dd")),
            ]
        )
    )
    story.append(t)

    story.append(Paragraph("Earnings", h2))
    rows = [["Component", "Full month (₹)", "Earned (₹)"]] + [
        [e["label"], _rs(e["monthly"]), _rs(e["amount"])] for e in figures["earnings"]
    ]
    for a in figures["adjustments"]:
        if Decimal(a["amount"]) >= 0:
            rows.append([a["label"], "", _rs(a["amount"])])
    rows.append(
        [
            "Total earnings",
            _rs(figures["gross_full"]),
            _rs(
                Decimal(figures["gross_earned"])
                + sum(
                    (
                        Decimal(a["amount"])
                        for a in figures["adjustments"]
                        if Decimal(a["amount"]) >= 0
                    ),
                    Decimal(0),
                )
            ),
        ]
    )
    story.append(_table(rows))

    story.append(Paragraph("Deductions", h2))
    drows = [["Component", "Amount (₹)"]] + [
        [x["label"], _rs(x["amount"])] for x in figures["deductions"]
    ]
    for a in figures["adjustments"]:
        if Decimal(a["amount"]) < 0:
            drows.append([a["label"], _rs(-Decimal(a["amount"]))])
    if len(drows) == 1:
        drows.append(["None", "0.00"])
    drows.append(["Total deductions", _rs(figures["total_deductions"])])
    story.append(_table(drows, cols=2))

    net = Table([["Net pay", f"₹ {_rs(figures['net_pay'])}"]], colWidths=[100 * mm, 74 * mm])
    net.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e6f6fa")),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story += [Spacer(1, 8), net]

    if figures["reimbursements"]:
        story.append(Paragraph("Reimbursements (paid with this salary, shown separately)", h2))
        rrows = [["Claim", "Date", "Amount (₹)"]] + [
            [r["label"], r["date"], _rs(r["amount"])] for r in figures["reimbursements"]
        ]
        rrows.append(["Total reimbursements", "", _rs(figures["reimbursement_total"])])
        story.append(_table(rrows))
        story.append(Paragraph(f"Total payable: ₹ {_rs(figures['payable_total'])}", body))

    if figures["employer"]:
        story.append(Paragraph("Employer contributions (not deducted from you)", h2))
        erows = [["Contribution", "Amount (₹)"]] + [
            [x["label"], _rs(x["amount"])] for x in figures["employer"]
        ]
        story.append(_table(erows, cols=2))

    story += [
        Spacer(1, 10),
        Paragraph(
            "Income tax (TDS) is not worked out on this payslip. This is a computer-generated payslip.",
            small,
        ),
    ]
    doc.build(story)
    return out.getvalue()


def _table(rows: list[list[str]], cols: int = 3) -> Table:
    widths = [100 * mm, 37 * mm, 37 * mm] if cols == 3 else [137 * mm, 37 * mm]
    t = Table(rows, colWidths=widths)
    t.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f4f7")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#eaecf0")),
                ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#98a2b3")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t
