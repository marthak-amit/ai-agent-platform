"""
SellerTalk24 billing API (Razorpay Orders API + Checkout.js, prepaid, manual renew).

Mounted at /billing (the dashboard reaches it as /api/billing — the dev proxy / hosting
rewrite strips /api). Razorpay's webhook is configured to call /billing/webhook directly.

  GET  /billing/plans         active plans with this tenant's base/GST/total
  GET  /billing/subscription  current period, usage, queued renewals, upgrade options + credit
  POST /billing/checkout      create the payment order -> Checkout.js options
  POST /billing/verify        browser callback: verify signature, activate (idempotent)
  POST /billing/webhook       server callback: signature-only auth, deduped, always 200
  GET  /billing/payments      tenant payment history (paginated; each row carries its invoice id/number)
  GET  /billing/invoices/{id}/pdf  the GST tax invoice as a PDF (Owner-only; 404 for another tenant's invoice)
  GET  /billing/alerts        dashboard billing alerts; POST /billing/alerts/{id}/read marks one read
  POST /billing/mock/complete mock mode only: simulate a successful payment

Money-moving routes are Owner-only; read-only routes accept any signed-in user of the tenant.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.models.client import Client
from app.models.sellertalk24_billing import Invoice, PaymentOrder
from app.routers.auth import get_current_client, get_owner_client
from app.schemas.sellertalk24_billing import (
    AlertListOut,
    AlertOut,
    CheckoutOut,
    CheckoutRequest,
    MockCompleteRequest,
    PaymentListOut,
    PlanListOut,
    PrefillOut,
    SubscriptionStateOut,
    VerifyRequest,
    WebhookAck,
)
from app.services.billing import alerts as billing_alerts
from app.services.billing import ops as billing_ops
from app.services.billing.activation import ActivationError, activate_from_payment
from app.services.billing.invoice_pdf import render_invoice_pdf
from app.services.billing.checkout import MERCHANT_NAME, create_checkout
from app.services.billing.razorpay_client import (
    BillingConfigError,
    BillingGateway,
    get_billing_gateway,
)
from app.services.billing.subscriptions import CheckoutError
from app.services.billing.views import (
    amounts_out,
    build_payment_list,
    build_plan_list,
    build_subscription_state,
)
from app.services.billing.webhook import (
    InvalidWebhookPayload,
    InvalidWebhookSignature,
    process_webhook,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


def get_gateway(settings: Annotated[Settings, Depends(get_settings)]) -> BillingGateway:
    """Dependency: the real or mock Razorpay gateway (overridable in tests)."""
    try:
        return get_billing_gateway(settings)
    except BillingConfigError as exc:
        logger.error("billing misconfigured: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing is temporarily unavailable.") from exc


def _http(exc: CheckoutError | ActivationError) -> HTTPException:
    """Map a billing error to an HTTPException with a stable {code, message} detail."""
    return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})


async def _tenant_order(db: AsyncSession, client: Client, razorpay_order_id: str) -> PaymentOrder:
    """The order if it belongs to this tenant; otherwise 404 (never reveal another tenant's order)."""
    order = (
        await db.execute(
            select(PaymentOrder).where(
                PaymentOrder.razorpay_order_id == razorpay_order_id, PaymentOrder.client_id == client.id
            )
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "order_not_found", "message": "Unknown order."})
    return order


async def _verify_and_activate(
    db: AsyncSession, gateway: BillingGateway, settings: Settings, client: Client,
    order_id: str, payment_id: str, signature: str, source: str,
) -> SubscriptionStateOut:
    """Shared by /verify and /mock/complete: ownership + signature check, then the idempotent activation."""
    await _tenant_order(db, client, order_id)
    if not gateway.verify_payment_signature(order_id, payment_id, signature):
        logger.warning("billing: invalid payment signature for order %s (client %s, %s)", order_id, client.id, source)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"code": "invalid_signature", "message": "Invalid payment signature."}
        )
    try:
        await activate_from_payment(
            db, gateway, razorpay_order_id=order_id, razorpay_payment_id=payment_id,
            signature=signature, source=source,
        )
    except ActivationError as exc:
        raise _http(exc) from exc
    return await build_subscription_state(db, client, settings)


@router.get("/plans", response_model=PlanListOut)
async def list_plans(
    client: Annotated[Client, Depends(get_current_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: AsyncSession = Depends(get_db),
) -> PlanListOut:
    """Active plans in order, with base/GST/total computed for the signed-in tenant."""
    return await build_plan_list(db, client, settings)


@router.get("/subscription", response_model=SubscriptionStateOut)
async def get_subscription(
    client: Annotated[Client, Depends(get_current_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: AsyncSession = Depends(get_db),
) -> SubscriptionStateOut:
    """Current subscription (usage, days left), queued renewals and upgrade options with credit preview."""
    return await build_subscription_state(db, client, settings)


@router.post("/checkout", response_model=CheckoutOut)
async def checkout(
    body: CheckoutRequest,
    client: Annotated[Client, Depends(get_owner_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    gateway: Annotated[BillingGateway, Depends(get_gateway)],
    db: AsyncSession = Depends(get_db),
) -> CheckoutOut:
    """
    Start a purchase. Decides new / renewal / upgrade, blocks a mid-cycle downgrade (400),
    creates the payment order + Razorpay order and returns the Checkout.js options.
    Rate limited to 10 orders per tenant per minute (429).
    """
    try:
        result = await create_checkout(db, gateway, settings, client, body.plan_code)
    except CheckoutError as exc:
        raise _http(exc) from exc

    order = result.order
    return CheckoutOut(
        key_id=result.key_id,
        razorpay_order_id=order.razorpay_order_id,
        amount=order.amount_paise,
        currency=order.currency,
        name=MERCHANT_NAME,
        description=result.description,
        prefill=PrefillOut(name=client.business_name or "", email=client.email, contact=client.phone or ""),
        mock=result.mock,
        payment_order_id=order.id,
        purpose=order.purpose,
        plan_code=result.quote.plan.code,
        amounts=amounts_out(result.quote.breakdown),
    )


@router.post("/verify", response_model=SubscriptionStateOut)
async def verify_payment(
    body: VerifyRequest,
    client: Annotated[Client, Depends(get_owner_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    gateway: Annotated[BillingGateway, Depends(get_gateway)],
    db: AsyncSession = Depends(get_db),
) -> SubscriptionStateOut:
    """
    Checkout.js success callback. Verifies the signature and the tenant's ownership of the order,
    then activates via the shared, idempotent activation (safe if the webhook arrives too).
    """
    return await _verify_and_activate(
        db, gateway, settings, client,
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature, "verify",
    )


@router.post("/mock/complete", response_model=SubscriptionStateOut)
async def mock_complete(
    body: MockCompleteRequest,
    client: Annotated[Client, Depends(get_owner_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    gateway: Annotated[BillingGateway, Depends(get_gateway)],
    db: AsyncSession = Depends(get_db),
) -> SubscriptionStateOut:
    """Mock mode only (404 otherwise): simulate the customer paying — valid mock signature, then /verify."""
    if not gateway.is_mock:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    await _tenant_order(db, client, body.razorpay_order_id)
    paid = gateway.simulate_payment(body.razorpay_order_id)  # type: ignore[attr-defined]
    return await _verify_and_activate(
        db, gateway, settings, client,
        paid["razorpay_order_id"], paid["razorpay_payment_id"], paid["razorpay_signature"], "mock",
    )


@router.post("/webhook", response_model=WebhookAck)
async def razorpay_webhook(
    request: Request,
    gateway: Annotated[BillingGateway, Depends(get_gateway)],
    db: AsyncSession = Depends(get_db),
) -> WebhookAck:
    """
    Razorpay server-to-server callback. NO login: the X-Razorpay-Signature over the raw body is the
    authentication (400 if invalid). Events are persisted and deduplicated on X-Razorpay-Event-Id;
    after that the answer is always HTTP 200 — handling errors are stored on the event row.
    """
    raw_body = await request.body()
    try:
        outcome = await process_webhook(
            db, gateway, raw_body,
            request.headers.get("X-Razorpay-Signature"),
            request.headers.get("X-Razorpay-Event-Id"),
        )
    except InvalidWebhookSignature as exc:
        logger.warning("billing webhook: invalid signature rejected")
        await billing_ops.record_bad_signature(db)       # burst => operator alert; never raises
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature.") from exc
    except InvalidWebhookPayload as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid payload.") from exc
    return WebhookAck(status=outcome.status)


@router.get("/alerts", response_model=AlertListOut)
async def list_billing_alerts(
    client: Annotated[Client, Depends(get_current_client)],
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> AlertListOut:
    """Dashboard billing alerts (usage thresholds, expiry reminders, grace/expired), newest first."""
    rows = await billing_alerts.list_alerts(db, client.id, unread_only=unread_only, limit=limit)
    unread = sum(1 for a in rows if a.read_at is None) if not unread_only else len(rows)
    return AlertListOut(alerts=[AlertOut.model_validate(a) for a in rows], unread=unread)


@router.post("/alerts/{alert_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_billing_alert_read(
    alert_id: int,
    client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Mark one of the tenant's alerts as read (404 if it is not theirs)."""
    ok = await billing_alerts.mark_read(db, client.id, alert_id)
    await db.commit()
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "alert_not_found", "message": "Unknown alert."})


@router.get("/payments", response_model=PaymentListOut)
async def list_payments(
    client: Annotated[Client, Depends(get_owner_client)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> PaymentListOut:
    """The tenant's payment orders, newest first, paginated."""
    return await build_payment_list(db, client.id, page, page_size)


@router.get("/invoices/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: int,
    client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Render the tenant's invoice as a PDF attachment (regenerated from the stored invoice row every time)."""
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id, Invoice.client_id == client.id))
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "invoice_not_found", "message": "Unknown invoice."})
    filename = invoice.invoice_number.replace("/", "-") + ".pdf"
    return Response(
        content=render_invoice_pdf(invoice),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"},
    )
