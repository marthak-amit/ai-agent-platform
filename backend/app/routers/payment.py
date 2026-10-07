"""
Razorpay payment router.

Handles:
- POST /payments/qr          : create a UPI QR code for a customer
- GET  /payments/{id}/invoice : download invoice PDF for a paid payment
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.payment import Payment
from app.services import invoice_service, razorpay_service

# Invoices are saved locally under backend/invoices/ and served via a URL.
_INVOICES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "invoices"
)
os.makedirs(_INVOICES_DIR, exist_ok=True)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


async def _owner_for_order(db: AsyncSession, order):
    """
    Return the active Client that owns `order`, or None.

    A Payment row carries no client_id, so the linked Order (which does) is the
    only trustworthy tenant attribution. There is deliberately no "first active
    client" fallback: with no linked order the tenant is unknown, and acting on
    another tenant's behalf (their WhatsApp credentials, GSTIN, business name)
    would leak data across tenants.
    """
    if order is None or order.client_id is None:
        return None
    from app.models.client import Client

    result = await db.execute(
        select(Client).where(Client.id == order.client_id, Client.is_active == True).limit(1)  # noqa: E712
    )
    return result.scalar_one_or_none()


class CreateQRRequest(BaseModel):
    """Request body for creating a Razorpay QR code."""

    phone_number: str
    amount: int
    description: str = "Payment"


@router.post("/qr", status_code=status.HTTP_201_CREATED)
async def create_payment_qr(
    body: CreateQRRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Create a Razorpay UPI QR code and persist the payment record.

    Args:
        body: phone_number, amount (in paise), description.
        db:   Injected async DB session.

    Returns:
        Dict with qr_code_id, image_url, short_url, and amount.

    Raises:
        HTTPException 502: If Razorpay API call fails.
    """
    try:
        qr_data = await razorpay_service.create_qr_code(
            amount=body.amount,
            description=body.description,
            phone_number=body.phone_number,
        )
    except Exception as exc:
        logger.error("Razorpay QR creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create QR code.",
        ) from exc

    payment = Payment(
        phone_number=body.phone_number,
        qr_code_id=qr_data["id"],
        amount=body.amount,
        description=body.description,
        status="created",
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    return {
        "qr_code_id": qr_data["id"],
        "image_url": qr_data.get("image_url"),
        "short_url": qr_data.get("short_url"),
        "amount": body.amount,
    }


@router.get("/{payment_id}/invoice", status_code=status.HTTP_200_OK)
async def download_invoice(
    payment_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Download the GST invoice PDF for a paid payment.

    Args:
        payment_id: Payment row ID.
        db:         Injected async DB session.

    Returns:
        PDF bytes with Content-Type application/pdf.

    Raises:
        HTTPException 404: If payment not found or invoice not yet generated.
    """
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")

    if payment.invoice_url is None:
        # Invoice not yet generated — generate on demand.
        try:
            from app.models.order import Order

            order = None
            if payment.razorpay_payment_id:
                order_result = await db.execute(
                    select(Order).where(
                        Order.razorpay_payment_id == payment.razorpay_payment_id
                    ).limit(1)
                )
                order = order_result.scalar_one_or_none()
            owner = await _owner_for_order(db, order)
            if owner is None:
                # Never render an invoice with another tenant's business/GSTIN details.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Invoice unavailable: payment is not linked to an order.",
                )
            amount_inr = payment.amount / 100
            products_for_invoice = [
                {
                    "name": payment.description or "Product",
                    "qty": 1,
                    "price": amount_inr / 1.05,
                    "hsn": owner.hsn_code if owner else "5007",
                }
            ]
            pdf_bytes = invoice_service.generate_gst_invoice(
                order_id=payment.razorpay_payment_id or str(payment.id),
                business_name=owner.business_name if owner else "",
                business_gst=owner.gst_number if (owner and owner.gst_number) else "N/A",
                business_address=owner.business_address if (owner and owner.business_address) else "",
                customer_name=payment.customer_name or payment.phone_number,
                customer_phone=payment.phone_number,
                customer_address=payment.customer_address or "",
                products=products_for_invoice,
                payment_method="UPI",
            )
            filename = f"invoice_{payment.id}.pdf"
            filepath = os.path.join(_INVOICES_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(pdf_bytes)
            payment.invoice_url = f"/invoices/{filename}"
            await db.commit()
        except Exception as exc:
            logger.error("On-demand invoice generation failed for %s: %s", payment_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invoice generation failed.",
            ) from exc

    filename = f"invoice_{payment.id}.pdf"
    filepath = os.path.join(_INVOICES_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice file not found.")

    with open(filepath, "rb") as f:
        pdf_bytes = f.read()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice_{payment_id}.pdf"'},
    )
