from datetime import datetime
import io
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def _money(currency: str, amount: Any) -> str:
    return f"{currency} {float(amount or 0):,.2f}"


def render_invoice_pdf(invoice: Dict[str, Any], business_name: str, draft: bool) -> bytes:
    """Renders an invoice row (from the invoices table) as a one-page A4 PDF.

    Drafts carry a large diagonal "DRAFT" watermark; approved invoices do not.
    """
    buffer = io.BytesIO()
    # Uncompressed: a one-page invoice stays small, and its text remains searchable/verifiable.
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    width, height = A4
    currency = invoice.get("currency") or "AED"
    left, right = 20 * mm, width - 20 * mm

    pdf.setTitle(f"{invoice['invoice_number']}{' (DRAFT)' if draft else ''}")
    pdf.setAuthor(business_name)

    if draft:
        pdf.saveState()
        pdf.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.15))
        pdf.setFont("Helvetica-Bold", 110)
        pdf.translate(width / 2, height / 2)
        pdf.rotate(35)
        pdf.drawCentredString(0, -40, "DRAFT")
        pdf.restoreState()

    y = height - 25 * mm
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(left, y, business_name)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawRightString(right, y, "TAX INVOICE")

    y -= 12 * mm
    pdf.setFont("Helvetica", 10)
    created = str(invoice.get("created_at") or datetime.utcnow().strftime("%Y-%m-%d"))[:10]
    status = "DRAFT - not valid for payment" if draft else "Approved"
    for label, value in [
        ("Invoice number", invoice["invoice_number"]),
        ("Date", created),
        ("Status", status),
    ]:
        pdf.drawRightString(right - 45 * mm, y, f"{label}:")
        pdf.drawString(right - 43 * mm, y, str(value))
        y -= 6 * mm

    y -= 4 * mm
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Bill to")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(left, y - 6 * mm, str(invoice.get("client_name") or ""))
    if invoice.get("client_contact"):
        pdf.drawString(left, y - 12 * mm, str(invoice["client_contact"]))

    y -= 26 * mm
    pdf.setFillColor(colors.HexColor("#F0F0F0"))
    pdf.rect(left, y - 2 * mm, right - left, 8 * mm, stroke=0, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 2 * mm, y, "Description")
    pdf.drawRightString(right - 2 * mm, y, "Amount (incl. VAT)")

    y -= 10 * mm
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left + 2 * mm, y, str(invoice.get("description") or "Services")[:80])
    pdf.drawRightString(right - 2 * mm, y, _money(currency, invoice.get("grand_total")))

    y -= 6 * mm
    pdf.line(left, y, right, y)
    y -= 8 * mm
    rows = [
        ("Net amount", invoice.get("subtotal")),
        ("VAT (5%)", invoice.get("tax_amount")),
        ("Total", invoice.get("grand_total")),
    ]
    if float(invoice.get("required_deposit") or 0) > 0:
        rows += [
            (f"Deposit ({float(invoice.get('deposit_percentage') or 0):.0f}%)", invoice.get("required_deposit")),
            ("Balance due", invoice.get("balance_due")),
        ]
    for label, amount in rows:
        bold = label == "Total"
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        pdf.drawRightString(right - 45 * mm, y, label)
        pdf.drawRightString(right - 2 * mm, y, _money(currency, amount))
        y -= 7 * mm

    pdf.setFont("Helvetica-Oblique", 8)
    pdf.setFillColor(colors.grey)
    footer = (
        "Draft prepared by the business assistant. Reply 'approve invoice "
        f"{invoice['invoice_number'].rsplit('-', 1)[-1]}' to finalise."
        if draft
        else "Thank you for your business."
    )
    pdf.drawString(left, 15 * mm, footer)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
