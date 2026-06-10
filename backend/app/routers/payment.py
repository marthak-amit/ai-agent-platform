"""
Razorpay payment router.

Handles:
- POST /payments/qr          : create a UPI QR code for a customer
- POST /payments/webhook      : handle Razorpay payment success/failure events
- GET  /payments/{id}/invoice : download invoice PDF for a paid payment
"""

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.payment import Payment
from app.services import invoice_service, razorpay_service, whatsapp_service

# Invoices are saved locally under backend/invoices/ and served via a URL.
_INVOICES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "invoices"
)
os.makedirs(_INVOICES_DIR, exist_ok=True)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


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


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle incoming Razorpay payment webhooks.

    Verifies X-Razorpay-Signature and marks the matching payment as paid.

    Args:
        request: Raw request (signature validation requires raw bytes).
        db:      Injected async DB session.

    Returns:
        {"status": "ok"} on success.

    Raises:
        HTTPException 401: If signature validation fails.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not razorpay_service.verify_webhook_signature(raw_body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Razorpay signature.",
        )

    try:
        event = json.loads(raw_body)
        event_type = event.get("event", "")
        payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        qr_code_id = payment_entity.get("acquirer_data", {}).get("upi_transaction_id")
        razorpay_payment_id = payment_entity.get("id")

        if event_type == "payment.captured":
            result = await db.execute(
                select(Payment).where(Payment.qr_code_id == qr_code_id)
            )
            payment = result.scalar_one_or_none()
            if payment:
                payment.status = "paid"
                payment.razorpay_payment_id = razorpay_payment_id
                await db.commit()
                logger.info("Payment %s marked as paid.", qr_code_id)

                # ── Mark linked order as paid ──────────────────────────────
                try:
                    from app.models.order import Order
                    from sqlalchemy import select as _select

                    order_result = await db.execute(
                        _select(Order).where(
                            Order.customer_phone == payment.phone_number,
                            Order.status == "payment_pending",
                        ).order_by(Order.created_at.desc()).limit(1)
                    )
                    linked_order = order_result.scalar_one_or_none()
                    if linked_order:
                        from datetime import datetime, timezone
                        linked_order.status = "paid"
                        linked_order.payment_status = "paid"
                        linked_order.razorpay_payment_id = razorpay_payment_id
                        linked_order.paid_at = datetime.now(timezone.utc)
                        await db.commit()
                        logger.info("Order %s marked paid via Razorpay.", linked_order.order_number)
                except Exception as exc:
                    logger.warning("Could not mark linked order as paid: %s", exc)

                # ── Notify customer ────────────────────────────────────────
                try:
                    amount_inr = payment.amount / 100
                    await whatsapp_service.send_text_message(
                        to_phone_number=payment.phone_number,
                        message_text=(
                            f"Payment received! ✅\n"
                            f"₹{amount_inr:.0f} confirmed. Your order is being packed "
                            f"and will be dispatched shortly. 🛍️\n\n"
                            f"Thank you for shopping with us! 🙏"
                        ),
                    )
                except Exception as exc:
                    logger.warning("Customer payment confirmation WhatsApp failed: %s", exc)

                # ── Notify owner ───────────────────────────────────────────
                try:
                    from app.models.client import Client

                    owner_result = await db.execute(
                        select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
                    )
                    owner = owner_result.scalar_one_or_none()
                    if owner and owner.phone:
                        amount_inr = payment.amount / 100
                        order_ref = linked_order.order_number if linked_order else (qr_code_id or "?")
                        await whatsapp_service.send_text_message(
                            to_phone_number=owner.phone,
                            message_text=(
                                f"💰 Payment received!\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"Order: {order_ref}\n"
                                f"Amount: ₹{amount_inr:.0f}\n"
                                f"From: {payment.phone_number}\n"
                                f"Razorpay ID: {razorpay_payment_id or 'N/A'}\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"Please pack and dispatch. 📦"
                            ),
                        )
                except Exception as exc:
                    logger.warning("Owner payment alert failed: %s", exc)

                # ── Generate GST invoice ───────────────────────────────────
                try:
                    from app.models.client import Client

                    owner_result = await db.execute(
                        select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
                    )
                    owner = owner_result.scalar_one_or_none()
                    amount_inr = payment.amount / 100
                    products_for_invoice = [
                        {
                            "name": payment.description or "Product",
                            "qty": 1,
                            "price": amount_inr / 1.05,  # back-calculate pre-GST price
                            "hsn": owner.hsn_code if owner else "5007",
                        }
                    ]

                    pdf_bytes = invoice_service.generate_gst_invoice(
                        order_id=razorpay_payment_id or qr_code_id or str(payment.id),
                        business_name=owner.business_name if owner else "Riya Sarees",
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

                    invoice_url = f"/invoices/{filename}"
                    payment.invoice_url = invoice_url
                    await db.commit()

                    await whatsapp_service.send_text_message(
                        to_phone_number=payment.phone_number,
                        message_text=(
                            f"Your GST invoice is ready 🧾\n"
                            f"Download: {invoice_url}\n"
                            f"Order ID: {razorpay_payment_id or qr_code_id}"
                        ),
                    )
                    logger.info("Invoice generated and sent for payment %s.", payment.id)
                except Exception as exc:
                    logger.error("Invoice generation error for payment %s: %s", payment.id, exc)

        elif event_type == "payment.failed":
            result = await db.execute(
                select(Payment).where(Payment.qr_code_id == qr_code_id)
            )
            payment = result.scalar_one_or_none()
            if payment:
                payment.status = "failed"
                payment.razorpay_payment_id = razorpay_payment_id
                await db.commit()
                logger.warning(
                    "Payment %s failed. Razorpay ID: %s. Customer: %s.",
                    qr_code_id,
                    razorpay_payment_id,
                    payment.phone_number,
                )

                # Notify customer
                try:
                    amount_inr = payment.amount // 100
                    await whatsapp_service.send_text_message(
                        to_phone_number=payment.phone_number,
                        message_text=(
                            f"Hi! Your payment of ₹{amount_inr} could not be processed. "
                            f"Please try again or contact us for assistance."
                        ),
                    )
                except Exception as exc:
                    logger.error(
                        "Could not send payment-failed WhatsApp to %s: %s",
                        payment.phone_number, exc,
                    )

                # Notify business owner
                try:
                    from app.models.client import Client
                    owner_result = await db.execute(
                        select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
                    )
                    owner = owner_result.scalar_one_or_none()
                    if owner and owner.whatsapp_number:
                        await whatsapp_service.send_text_message(
                            to_phone_number=owner.whatsapp_number,
                            message_text=(
                                f"Payment failed: ₹{amount_inr} from {payment.phone_number}. "
                                f"Razorpay ID: {razorpay_payment_id or 'unknown'}."
                            ),
                        )
                except Exception as exc:
                    logger.error("Could not send payment-failed owner alert: %s", exc)

    except Exception as exc:
        logger.error("Error processing Razorpay webhook: %s", exc)

    return {"status": "ok"}


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
            from app.models.client import Client

            owner_result = await db.execute(
                select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
            )
            owner = owner_result.scalar_one_or_none()
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
