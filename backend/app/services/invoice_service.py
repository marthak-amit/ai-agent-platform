"""
GST invoice PDF generation service.

Generates a compliant Indian GST Tax Invoice as a PDF using ReportLab.
GST rate is fixed at 5% (HSN 5007 — woven fabrics of silk / textile default).
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def generate_gst_invoice(
    order_id: str,
    business_name: str,
    business_gst: str,
    business_address: str,
    customer_name: str,
    customer_phone: str,
    customer_address: str,
    products: list[dict],
    payment_method: str = "UPI",
) -> bytes:
    """
    Generate a GST Tax Invoice PDF and return it as bytes.

    Args:
        order_id:         Unique order / payment identifier.
        business_name:    Seller's registered business name.
        business_gst:     Seller's GSTIN (15-character).
        business_address: Seller's registered address.
        customer_name:    Buyer's display name.
        customer_phone:   Buyer's WhatsApp / phone number.
        customer_address: Buyer's delivery address.
        products:         List of dicts with keys:
                            name (str), qty (int), price (float),
                            hsn (str, optional — defaults to '5007').
        payment_method:   Payment method string, e.g. 'UPI', 'COD'.

    Returns:
        PDF content as bytes.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # ── Header: business info ─────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, height - 50, business_name)
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 68, f"GSTIN: {business_gst}")
    c.drawString(50, height - 82, business_address)

    # ── Divider ───────────────────────────────────────────────────────────────
    c.setStrokeColor(colors.HexColor("#4F46E5"))
    c.setLineWidth(1.5)
    c.line(50, height - 95, width - 50, height - 95)

    # ── Invoice title + number ────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor("#4F46E5"))
    c.drawString(50, height - 115, "TAX INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(400, height - 115, f"Invoice #: {order_id}")
    c.drawString(400, height - 130, f"Date: {datetime.now().strftime('%d/%m/%Y')}")

    # ── Bill To ───────────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, height - 160, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 175, customer_name)
    c.drawString(50, height - 190, customer_phone)
    c.drawString(50, height - 205, customer_address)

    # ── Table header ─────────────────────────────────────────────────────────
    y = height - 245
    c.setFillColor(colors.HexColor("#F3F4F6"))
    c.rect(50, y - 5, width - 100, 20, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(55,  y + 5, "Product")
    c.drawString(285, y + 5, "HSN")
    c.drawString(335, y + 5, "Qty")
    c.drawString(375, y + 5, "Rate")
    c.drawString(425, y + 5, "GST 5%")
    c.drawString(485, y + 5, "Total")

    # ── Table rows ────────────────────────────────────────────────────────────
    y -= 20
    subtotal = 0.0
    total_gst = 0.0
    c.setFont("Helvetica", 10)

    for product in products:
        rate = float(product["price"])
        qty = int(product["qty"])
        gst = round(rate * qty * 0.05, 2)
        total = round(rate * qty + gst, 2)
        subtotal += rate * qty
        total_gst += gst

        c.drawString(55,  y, product["name"][:35])
        c.drawString(285, y, str(product.get("hsn", "5007")))
        c.drawString(335, y, str(qty))
        c.drawString(375, y, f"₹{rate:.2f}")
        c.drawString(425, y, f"₹{gst:.2f}")
        c.drawString(485, y, f"₹{total:.2f}")
        y -= 20

    # ── Totals ────────────────────────────────────────────────────────────────
    y -= 8
    c.setLineWidth(0.5)
    c.line(360, y + 14, width - 50, y + 14)

    c.setFont("Helvetica", 10)
    c.drawString(370, y,      "Subtotal:")
    c.drawString(485, y,      f"₹{subtotal:.2f}")
    y -= 18
    c.drawString(370, y,      "GST (5%):")
    c.drawString(485, y,      f"₹{total_gst:.2f}")
    y -= 4
    c.line(360, y, width - 50, y)
    y -= 16
    c.setFont("Helvetica-Bold", 12)
    c.drawString(370, y,      "TOTAL:")
    c.drawString(485, y,      f"₹{round(subtotal + total_gst, 2):.2f}")

    # ── Footer ────────────────────────────────────────────────────────────────
    y -= 45
    c.setFont("Helvetica", 10)
    c.drawString(50, y,      f"Payment method: {payment_method}")
    c.drawString(50, y - 16, "Thank you for your business!")
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#6B7280"))
    c.drawString(50, 40,     "This is a computer-generated invoice and does not require a signature.")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
