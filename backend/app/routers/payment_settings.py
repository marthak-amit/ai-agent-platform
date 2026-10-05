"""
Seller payment settings for manual UPI verification.

Endpoints:
- GET    /settings/payment      : UPI ID, payee name, static QR, bot/expiry knobs, message preview
- PUT    /settings/payment      : update them (Owner only)
- POST   /settings/payment/qr   : upload a static QR image (Owner only)
- DELETE /settings/payment/qr   : remove it (Owner only)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client, get_owner_client
from app.services import media_service
from app.services import payment_verification_service as pvs
from app.services.language_templates import format_price, get_template

router = APIRouter(prefix="/settings/payment", tags=["payment-settings"])

_QR_MAX_BYTES = 3 * 1024 * 1024
_PREVIEW_LANGS = {"en": "english", "hi": "hindi_devanagari", "gu": "gujarati_script"}


class PaymentSettingsOut(BaseModel):
    """Payment settings as shown on Settings → Payment details."""

    upi_id: Optional[str]
    upi_payee_name: Optional[str]
    upi_qr_url: Optional[str]
    upi_configured: bool
    setup_alert_active: bool
    bot_auto_resume_minutes: int
    payment_expiry_hours: int
    preview: dict[str, str] = Field(description="Message a customer receives, keyed by en/hi/gu.")


class PaymentSettingsUpdate(BaseModel):
    """PUT body — every field optional; omitted fields are left unchanged."""

    upi_id: Optional[str] = None
    upi_payee_name: Optional[str] = Field(default=None, max_length=100)
    bot_auto_resume_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    payment_expiry_hours: Optional[int] = Field(default=None, ge=1, le=720)


def _preview(client: Client) -> dict[str, str]:
    """Render the exact payment message a customer would get, for a sample order."""
    out = {}
    for code, lang in _PREVIEW_LANGS.items():
        text = get_template(
            lang, "pay_instruction",
            order_number="ORD-2026-0001",
            items="• Sample Kurta (Blue/M) × 1 = " + format_price(1299),
            amount=format_price(1299),
            upi_id=client.upi_id or "yourname@bank",
            payee_line=f"\n{client.upi_display_name}" if client.upi_display_name else "",
            extra=f"\n{client.payment_instructions}" if client.payment_instructions else "",
        )
        out[code] = f"{text}\n[UPI QR image]\n{get_template(lang, 'pay_send_screenshot')}"
    return out


def _out(client: Client) -> PaymentSettingsOut:
    """Serialise a client's payment settings."""
    return PaymentSettingsOut(
        upi_id=client.upi_id,
        upi_payee_name=client.upi_display_name,
        upi_qr_url=client.upi_qr_url,
        upi_configured=bool(client.upi_id),
        setup_alert_active=client.payment_setup_alert_at is not None and not client.upi_id,
        bot_auto_resume_minutes=client.bot_auto_resume_minutes,
        payment_expiry_hours=client.payment_expiry_hours,
        preview=_preview(client),
    )


@router.get("", response_model=PaymentSettingsOut)
async def get_payment_settings(client: Client = Depends(get_current_client)) -> PaymentSettingsOut:
    """Return the seller's payment settings and the customer-facing message preview."""
    return _out(client)


@router.put("", response_model=PaymentSettingsOut)
async def update_payment_settings(
    body: PaymentSettingsUpdate,
    client: Client = Depends(get_owner_client),
    db: AsyncSession = Depends(get_db),
) -> PaymentSettingsOut:
    """
    Update UPI ID / payee name / auto-resume / expiry.

    Raises:
        HTTPException 422: upi_id is not in name@handle form.
    """
    fields = body.model_dump(exclude_unset=True)
    if "upi_id" in fields:
        upi = (fields["upi_id"] or "").strip()
        if upi and not pvs.is_valid_upi_id(upi):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "INVALID_UPI_ID", "message": "UPI ID must look like name@handle."},
            )
        client.upi_id = upi or None
        if upi:
            client.payment_setup_alert_at = None
    if "upi_payee_name" in fields:
        client.upi_display_name = (fields["upi_payee_name"] or "").strip() or None
    if fields.get("bot_auto_resume_minutes") is not None:
        client.bot_auto_resume_minutes = fields["bot_auto_resume_minutes"]
    if fields.get("payment_expiry_hours") is not None:
        client.payment_expiry_hours = fields["payment_expiry_hours"]
    await db.commit()
    await db.refresh(client)
    return _out(client)


@router.post("/qr", response_model=PaymentSettingsOut)
async def upload_static_qr(
    file: UploadFile = File(...),
    client: Client = Depends(get_owner_client),
    db: AsyncSession = Depends(get_db),
) -> PaymentSettingsOut:
    """Upload the seller's own static UPI QR (fallback when a dynamic QR can't be generated)."""
    data = await file.read()
    if len(data) > _QR_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="QR image too large (max 3 MB).")
    ctype = media_service.sniff_content_type(data)
    if ctype not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="QR must be a JPEG, PNG or WebP image.")
    client.upi_qr_url = await media_service.store_media(client.id, data, ctype, folder="upi-qr")
    await db.commit()
    await db.refresh(client)
    return _out(client)


@router.delete("/qr", response_model=PaymentSettingsOut)
async def delete_static_qr(
    client: Client = Depends(get_owner_client),
    db: AsyncSession = Depends(get_db),
) -> PaymentSettingsOut:
    """Remove the stored static QR."""
    client.upi_qr_url = None
    await db.commit()
    await db.refresh(client)
    return _out(client)
