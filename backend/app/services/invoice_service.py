"""
Invoice PDF generation service.

Two independent invoice styles live here:
- generate_gst_invoice(): compliant Indian GST Tax Invoice for the Razorpay/
  UPI QR Payment flow (app/routers/payment.py). GST rate fixed at 5%.
- generate_order_invoice(): branded order-confirmation invoice for the
  order_pipeline/order_service flow (app/models/order.py). No GST math —
  formats exactly what's already captured on the Order row.

Both render with ReportLab and are saved as local files under backend/invoices/,
served via the /invoices StaticFiles mount (app/main.py).
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Optional

import httpx
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.order import Order

# Same directory/convention as app/routers/payment.py's _INVOICES_DIR.
_INVOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "invoices")
_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")


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


# ── Order confirmation invoice ────────────────────────────────────────────────

async def generate_invoice_number(db: AsyncSession, client_id: int) -> str:
    """
    Return the next sequential invoice number for a client: INV-{client_id}-{seq}.

    Sequence is per-client (unlike Order.order_number, which is global) —
    counts existing invoiced orders for this client and adds one.

    Args:
        db:        Active async DB session.
        client_id: Owning client's primary key.

    Returns:
        Invoice number string, e.g. "INV-7-0001".
    """
    result = await db.execute(
        select(func.count()).select_from(Order).where(
            Order.client_id == client_id,
            Order.invoice_number.isnot(None),
        )
    )
    count = result.scalar_one() or 0
    return f"INV-{client_id}-{str(count + 1).zfill(4)}"


async def load_client_logo_bytes(client) -> Optional[bytes]:
    """
    Best-effort fetch of a client's logo image bytes for embedding in a PDF.

    Never raises — a missing or unreachable logo must not block invoice
    generation. Handles both locally-stored ("/uploads/...") and externally
    hosted (http/https) logo_url values.

    Args:
        client: Client ORM instance.

    Returns:
        Raw image bytes, or None if no logo is set or it can't be loaded.
    """
    logo_url = getattr(client, "logo_url", None)
    if not logo_url:
        return None

    try:
        if logo_url.startswith("/uploads/"):
            filepath = os.path.join(_UPLOADS_DIR, os.path.basename(logo_url))
            if not os.path.exists(filepath):
                return None
            with open(filepath, "rb") as f:
                return f.read()

        if logo_url.startswith("http://") or logo_url.startswith("https://"):
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                response = await http_client.get(logo_url)
                response.raise_for_status()
                return response.content
    except Exception:
        return None

    return None


def _variant_line(order: Order) -> str:
    """Join non-empty variant fields into a display string, e.g. 'Blue / M'."""
    parts = [p for p in [order.variant_color, order.variant_size, order.variant_material] if p]
    return " / ".join(parts)


def _payment_status_line(order: Order) -> str:
    """Human-readable payment status, e.g. 'Cash on Delivery' or 'Paid via UPI'."""
    if order.payment_method == "COD":
        return "Paid on Delivery (Cash)" if order.payment_status == "paid" else "Cash on Delivery"
    return "Paid via UPI" if order.payment_status == "paid" else f"Payment pending ({order.payment_method})"


def generate_order_invoice(
    order: Order,
    client,
    invoice_number: str,
    logo_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Generate a branded order-confirmation invoice PDF and return it as bytes.

    Formats data already captured on the Order/Client rows — no GST math,
    no new data entry. Delivery is always shown as Free (no delivery-fee
    concept exists in this platform today). The itemized table has exactly
    one row, matching the Order model (one product per order).

    Args:
        order:          Order ORM instance (paid or COD-confirmed).
        client:         Owning Client ORM instance (for branding/footer).
        invoice_number: Pre-generated sequential number, e.g. "INV-7-0001".
        logo_bytes:     Optional raw logo image bytes (see load_client_logo_bytes).

    Returns:
        PDF content as bytes.
    """
    from app.services.delivery_service import get_delivery_time_str
    from app.services.language_templates import format_price

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # ── Header: logo + business info ──────────────────────────────────────────
    text_x = 50
    if logo_bytes:
        try:
            logo_reader = ImageReader(io.BytesIO(logo_bytes))
            c.drawImage(
                logo_reader, 50, height - 105, width=55, height=55,
                preserveAspectRatio=True, mask="auto",
            )
            text_x = 115
        except Exception:
            text_x = 50

    c.setFont("Helvetica-Bold", 18)
    c.drawString(text_x, height - 50, client.business_name or "")
    c.setFont("Helvetica", 10)
    y_header = height - 68
    if getattr(client, "business_address", None):
        c.drawString(text_x, y_header, client.business_address)
        y_header -= 14
    if getattr(client, "gst_number", None):
        c.drawString(text_x, y_header, f"GSTIN: {client.gst_number}")

    # ── Divider ───────────────────────────────────────────────────────────────
    c.setStrokeColor(colors.HexColor("#4F46E5"))
    c.setLineWidth(1.5)
    c.line(50, height - 118, width - 50, height - 118)

    # ── Invoice title + number ────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor("#4F46E5"))
    c.drawString(50, height - 138, "INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(380, height - 138, f"Invoice #: {invoice_number}")
    c.drawString(380, height - 153, f"Order #: {order.order_number}")
    order_date = order.created_at.strftime("%d/%m/%Y") if order.created_at else datetime.now().strftime("%d/%m/%Y")
    c.drawString(380, height - 168, f"Date: {order_date}")

    # ── Bill To ───────────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, height - 185, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 200, order.customer_name)
    c.drawString(50, height - 215, order.customer_phone)
    c.drawString(50, height - 230, (order.delivery_address or "")[:70])

    # ── Table header ─────────────────────────────────────────────────────────
    y = height - 265
    c.setFillColor(colors.HexColor("#F3F4F6"))
    c.rect(50, y - 5, width - 100, 20, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(55,  y + 5, "Product")
    c.drawString(255, y + 5, "Variant")
    c.drawString(340, y + 5, "Qty")
    c.drawString(380, y + 5, "Unit Price")
    c.drawString(470, y + 5, "Total")

    # ── Table rows — one per cart line item (Phase 1 cart engine); falls
    # back to the single flat row for legacy pre-cart orders. ──────────────
    y -= 20
    c.setFont("Helvetica", 10)
    _line_items = list(getattr(order, "line_items", None) or [])
    if not _line_items:
        c.drawString(55,  y, order.product_name[:32])
        c.drawString(255, y, _variant_line(order)[:20] or "—")
        c.drawString(340, y, str(order.quantity))
        c.drawString(380, y, format_price(order.unit_price))
        c.drawString(470, y, format_price(order.total_amount))
        y -= 20
    else:
        for _li in _line_items:
            if y < 130:
                # Page-break guard: a batch-mode cart can have several
                # distinct variant rows, unlike the single-row legacy case.
                c.showPage()
                y = height - 60
                c.setFont("Helvetica", 10)
            _li_variant = " / ".join(
                p for p in [_li.variant_color, _li.variant_size, _li.variant_material] if p
            )
            c.drawString(55,  y, _li.product_name[:32])
            c.drawString(255, y, _li_variant[:20] or "—")
            c.drawString(340, y, str(_li.quantity))
            c.drawString(380, y, format_price(_li.unit_price))
            c.drawString(470, y, format_price(_li.subtotal))
            y -= 20

    if y < 110:
        c.showPage()
        y = height - 60

    # ── Totals ────────────────────────────────────────────────────────────────
    y -= 6
    c.setLineWidth(0.5)
    c.line(360, y + 4, width - 50, y + 4)
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(370, y, "Subtotal:")
    c.drawString(470, y, format_price(order.total_amount))
    y -= 16
    c.drawString(370, y, "Delivery:")
    c.drawString(470, y, "Free")
    y -= 6
    c.line(360, y, width - 50, y)
    y -= 16
    c.setFont("Helvetica-Bold", 12)
    c.drawString(370, y, "TOTAL:")
    c.drawString(470, y, format_price(order.total_amount))

    # ── Delivery estimate + payment status ────────────────────────────────────
    y -= 30
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Estimated delivery: {get_delivery_time_str(client=client)}")
    y -= 16
    c.drawString(50, y, f"Payment status: {_payment_status_line(order)}")

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_contact = getattr(client, "phone", None) or getattr(client, "whatsapp_number", None)
    if footer_contact:
        c.setFont("Helvetica", 9)
        c.drawString(50, 55, f"Contact: {footer_contact}")
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#6B7280"))
    c.drawString(50, 40, "This is a computer-generated invoice and does not require a signature.")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def save_order_invoice_pdf(order_id: int, pdf_bytes: bytes) -> str:
    """
    Write an order-invoice PDF to local disk and return its absolute URL.

    Uses the same backend/invoices/ directory as the Payment/GST invoice
    flow, with a distinct filename prefix to avoid collisions.

    Args:
        order_id:  Order row ID (used in the filename).
        pdf_bytes: PDF content to write.

    Returns:
        Absolute URL built from settings.backend_public_url, e.g.
        "https://app.example.com/invoices/order_invoice_42.pdf".
    """
    os.makedirs(_INVOICES_DIR, exist_ok=True)
    filename = f"order_invoice_{order_id}.pdf"
    filepath = os.path.join(_INVOICES_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(pdf_bytes)

    settings = get_settings()
    return f"{settings.backend_public_url}/invoices/{filename}"
