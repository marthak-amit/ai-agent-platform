"""
Checkout: turn "client X wants plan Y" into a payment_orders row + a Razorpay order.

Order of operations (spec): insert the payment_orders row (status=created) and flush to get
its id, THEN create the Razorpay order with that id in its notes, then store the Razorpay
order id on the row and commit. If the gateway call fails nothing is persisted.

The per-tenant rate limit is DB-derived (orders created in the last minute), so it holds
across workers with no in-memory state.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.client import Client
from app.models.sellertalk24_billing import PaymentOrder, PaymentOrderStatus
from app.services.billing.razorpay_client import BillingGateway, BillingGatewayError
from app.services.billing.subscriptions import (
    CheckoutError,
    Quote,
    build_quote,
    get_plan_by_code,
    now_utc,
    roll_forward,
)

logger = logging.getLogger(__name__)

CHECKOUTS_PER_MINUTE = 10
MOCK_KEY_ID = "rzp_test_mock"
MERCHANT_NAME = "SellerTalk24"


@dataclass(frozen=True)
class CheckoutResult:
    """A created order, ready for Checkout.js."""

    order: PaymentOrder
    quote: Quote
    key_id: str
    mock: bool
    description: str


async def enforce_checkout_rate_limit(db: AsyncSession, client_id: int, now: datetime) -> None:
    """Raise CheckoutError(429) if the client already created CHECKOUTS_PER_MINUTE orders in the last minute."""
    recent = await db.scalar(
        select(func.count()).select_from(PaymentOrder).where(
            PaymentOrder.client_id == client_id,
            PaymentOrder.created_at > now - timedelta(minutes=1),
        )
    )
    if (recent or 0) >= CHECKOUTS_PER_MINUTE:
        raise CheckoutError(
            "rate_limited", "Too many checkout attempts. Please wait a minute and try again.", http_status=429
        )


def _description(quote: Quote) -> str:
    """Human description shown in Checkout.js and on the Razorpay dashboard."""
    days = quote.plan.billing_period_days
    label = {"upgrade": " (upgrade)", "renewal": " (renewal)"}.get(quote.purpose, "")
    return f"SellerTalk24 {quote.plan.name} — {days} days{label}"


def _receipt(client_id: int) -> str:
    """Unique receipt "st24_<client>_<ms>_<hex>" (<= 40 chars, Razorpay's limit)."""
    return f"st24_{client_id}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"[:40]


async def create_checkout(
    db: AsyncSession,
    gateway: BillingGateway,
    settings: Settings,
    client: Client,
    plan_code: str,
    now: datetime | None = None,
) -> CheckoutResult:
    """
    Create the payment order for *client* buying *plan_code*.

    Raises:
        CheckoutError: unknown plan (404), mid-cycle downgrade (400), rate limit (429),
                       gateway failure (502).
    """
    now = now or now_utc()
    plan = await get_plan_by_code(db, plan_code)
    if plan is None:
        raise CheckoutError("unknown_plan", f"Unknown or unavailable plan: {plan_code!r}.", http_status=404)

    await enforce_checkout_rate_limit(db, client.id, now)
    await roll_forward(db, client.id, now)
    quote = await build_quote(db, client, plan, settings.seller_state_code, now, grace_days=settings.grace_days)
    b = quote.breakdown

    order = PaymentOrder(
        client_id=client.id,
        plan_id=plan.id,
        razorpay_order_id=f"pending_{secrets.token_hex(12)}",  # placeholder until Razorpay answers
        amount_paise=b.total_paise,
        base_paise=b.base_paise,
        gst_paise=b.gst_paise,
        credit_paise=b.credit_paise,
        taxable_paise=b.taxable_paise,
        cgst_paise=b.cgst_paise,
        sgst_paise=b.sgst_paise,
        igst_paise=b.igst_paise,
        currency=plan.currency,
        receipt=_receipt(client.id),
        status=PaymentOrderStatus.CREATED,
        purpose=quote.purpose,
        mode=gateway.mode,        # 'test' for the mock / rzp_test_ keys, 'live' for rzp_live_ — see activation
    )
    db.add(order)
    await db.flush()

    try:
        gateway_order = await gateway.create_order(
            b.total_paise,
            order.receipt,
            {"client_id": str(client.id), "plan_code": plan.code, "payment_order_id": str(order.id)},
        )
    except BillingGatewayError as exc:
        client_id = client.id          # read before rollback() expires the ORM object
        await db.rollback()
        logger.error("billing: Razorpay order creation failed for client %s: %s", client_id, exc)
        raise CheckoutError(
            "gateway_error", "Could not start the payment. Please try again in a moment.", http_status=502
        ) from exc

    order.razorpay_order_id = gateway_order["id"]
    await db.commit()
    logger.info(
        "billing: checkout order %s (%s) client=%s plan=%s total=%s paise",
        order.id, quote.purpose, client.id, plan.code, b.total_paise,
    )

    mock = bool(gateway.is_mock)
    return CheckoutResult(
        order=order, quote=quote, key_id=MOCK_KEY_ID if mock else settings.razorpay_key_id,
        mock=mock, description=_description(quote),
    )
