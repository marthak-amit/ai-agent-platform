"""
activate_from_payment — the ONE place a paid Razorpay order becomes a subscription.

Both /billing/verify (browser callback) and /billing/webhook (server callback) call it, and
both usually arrive for the same payment. It must therefore be:

  * ATOMIC      one transaction: lock the order row, write everything, commit once.
  * IDEMPOTENT  an order that is already paid returns its existing subscription; two
                simultaneous calls produce exactly one activation (the second blocks on
                the row lock, then sees status='paid').
  * VERIFIED    the payment is fetched from the gateway and must be captured, belong to this
                order, and match the order amount to the paisa — the caller's claims and
                webhook JSON are never trusted for money.

The network fetch happens BEFORE the row lock so the lock is held only for DB work.

Purchase rules (applied at activation time, with the lock held):
  new / renewal  the period starts when the last running/queued period ends (stacking); with
                 nothing running it starts now and is 'active', otherwise it is 'pending'.
  upgrade        the running period (and any queued ones) become 'superseded'; the new period
                 starts now, conversations_used is carried over, conversation_limit is
                 snapshotted from the new plan. An "upgrade" that is no longer an upgrade
                 (state changed since checkout) is stacked like a renewal instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.sellertalk24_billing import (
    ClientSubscription,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
    SubscriptionStatus,
)
from app.services.billing import invoices as invoice_service
from app.services.billing import ops
from app.services.billing.razorpay_client import BillingGateway, BillingGatewayError
from app.services.billing.subscriptions import (
    get_plan_by_id,
    list_pending_subscriptions,
    now_utc,
    roll_forward,
    sync_legacy_client_plan,
)

logger = logging.getLogger(__name__)


class ActivationError(Exception):
    """Activation refused. `code` is machine-readable; `retryable` means a later attempt may succeed."""

    def __init__(self, code: str, message: str, *, http_status: int = 400, retryable: bool = False) -> None:
        """Store the code, the message, the HTTP status to answer with and the retry hint."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of activate_from_payment."""

    subscription: ClientSubscription
    order: PaymentOrder
    already_processed: bool  # True when this call found the order already paid (no new activation)


async def _subscription_for_order(db: AsyncSession, order: PaymentOrder) -> ClientSubscription | None:
    """The subscription row created by *order*, if any."""
    return (
        await db.execute(
            select(ClientSubscription).where(ClientSubscription.source_payment_id == order.id)
        )
    ).scalar_one_or_none()


def _check_payment(payment: dict, razorpay_order_id: str) -> None:
    """Raise ActivationError unless the fetched payment is captured and belongs to the order."""
    if payment.get("order_id") != razorpay_order_id:
        raise ActivationError("payment_order_mismatch", "This payment does not belong to this order.")
    status = payment.get("status")
    if status == "captured":
        return
    if status == "authorized":
        # Auto-capture follows authorization within seconds; the webhook will activate the plan.
        raise ActivationError(
            "payment_not_captured",
            "Your payment was received and is being confirmed. Your plan will activate within a minute.",
            http_status=409, retryable=True,
        )
    if status == "failed":
        raise ActivationError("payment_failed", "The payment failed. Please try again.")
    if status == "refunded":
        raise ActivationError("payment_refunded", "This payment was refunded.")
    raise ActivationError("payment_not_captured", f"Payment is not captured (status: {status}).", http_status=409, retryable=True)


async def _report_amount_mismatch(db: AsyncSession, order_id: int, client_id: int, detail: str, source: str) -> None:
    """Count the mismatch for /health/billing and alert the operator once per order. Never raises."""
    try:
        await ops.record_ops_event(db, "amount_mismatch", order_id=order_id, client_id=client_id, detail=detail, source=source)
        await db.commit()
        await ops.send_admin_alert(
            db, "amount_mismatch", str(order_id), f"Amount mismatch on payment order {order_id}",
            f"Razorpay reported a different amount than we charged for payment order {order_id} "
            f"(client {client_id}, via {source}): {detail}. The order was marked failed and NOT activated. "
            "Check the payment in the Razorpay dashboard; refund it or grant the plan manually.",
        )
    except Exception:
        logger.exception("billing: could not report amount mismatch for order %s", order_id)


async def _apply_purchase(db: AsyncSession, order: PaymentOrder, now: datetime) -> ClientSubscription:
    """Create the subscription row for a verified, locked order (see the module docstring)."""
    plan = await get_plan_by_id(db, order.plan_id)
    client = await db.get(Client, order.client_id)
    if plan is None or client is None:
        raise ActivationError("invalid_state", "Plan or client for this order no longer exists.", http_status=500)

    active = await roll_forward(db, order.client_id, now)
    pending = await list_pending_subscriptions(db, order.client_id, for_update=True)
    period = timedelta(days=plan.billing_period_days)

    upgrade_from = None
    if order.purpose == PaymentPurpose.UPGRADE and active is not None:
        active_plan = await get_plan_by_id(db, active.plan_id)
        if active_plan is not None and plan.sort_order > active_plan.sort_order:
            upgrade_from = active

    if upgrade_from is not None:
        carried_usage = upgrade_from.conversations_used
        upgrade_from.status = SubscriptionStatus.SUPERSEDED
        for queued in pending:
            queued.status = SubscriptionStatus.SUPERSEDED
        await db.flush()  # free the single-active slot before inserting the new active row
        start, status, used = now, SubscriptionStatus.ACTIVE, carried_usage
    else:
        if order.purpose == PaymentPurpose.UPGRADE:
            logger.warning(
                "billing: upgrade order %s no longer applies (no lower running plan); stacking it. credit=%s",
                order.id, order.credit_paise,
            )
        ends = [now]
        if active is not None:
            ends.append(active.current_period_end)
        ends.extend(p.current_period_end for p in pending)
        start = max(ends)
        status = SubscriptionStatus.ACTIVE if start <= now else SubscriptionStatus.PENDING
        used = 0

    sub = ClientSubscription(
        client_id=order.client_id,
        plan_id=plan.id,
        status=status,
        current_period_start=start,
        current_period_end=start + period,
        conversation_limit=plan.conversation_limit,   # snapshot: later plan edits never change a paid period
        conversations_used=used,
        over_limit=used > plan.conversation_limit,    # carried-over usage can already exceed the new limit
        credited_paise=order.credit_paise,
        source_payment_id=order.id,
    )
    db.add(sub)
    await db.flush()

    if status == SubscriptionStatus.ACTIVE:
        await sync_legacy_client_plan(db, client, plan, now)
    return sub


async def _issue_invoice(db: AsyncSession, order: PaymentOrder, sub: ClientSubscription, now: datetime) -> None:
    """
    Issue the tax invoice in the activation transaction, so a paid order and its invoice commit together.

    Runs in a SAVEPOINT and never fails the activation: the customer has paid, so a problem here (bad
    seller config, ...) is logged and the invoice is created lazily later (billing.invoices.ensure_invoice /
    the admin backfill). The savepoint also rolls the counter increment back, so no number is wasted.
    """
    try:
        async with db.begin_nested():
            client = await db.get(Client, order.client_id)
            plan = await get_plan_by_id(db, order.plan_id)
            await invoice_service.create_invoice(db, order, client, plan, subscription_id=sub.id, now=now)
    except Exception:
        logger.exception("billing: invoice creation failed for order %s — activation continues", order.id)


async def activate_from_payment(
    db: AsyncSession,
    gateway: BillingGateway,
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    signature: str | None = None,
    source: str,
    now: datetime | None = None,
) -> ActivationResult:
    """
    Verify a payment with the gateway and activate the subscription it bought — exactly once.

    Args:
        db:                  Session; this function commits (success) or rolls back (failure).
        gateway:             Real or mock Razorpay client (used to fetch the payment).
        razorpay_order_id:   The Razorpay order being paid.
        razorpay_payment_id: The payment claimed against it.
        signature:           Checkout signature, stored on the order if given (the CALLER verifies it).
        source:              "verify" | "webhook" | "mock" — for logs only.
        now:                 Injected clock for tests.

    Returns:
        ActivationResult; already_processed=True when the order had been activated before.

    Raises:
        ActivationError: order unknown / payment not captured / amount or order mismatch /
                         gateway unreachable (retryable) / payment id reuse.
    """
    now = now or now_utc()
    try:
        result = await _activate(db, gateway, razorpay_order_id, razorpay_payment_id, signature, source, now)
    except ActivationError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise
    if not result.already_processed:
        # Exactly once per payment (the activation itself is). notify_* never raises.
        from app.services.billing import emails

        await emails.notify_payment_success(db, result.order.id)
    return result


async def _activate(
    db: AsyncSession,
    gateway: BillingGateway,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    signature: str | None,
    source: str,
    now: datetime,
) -> ActivationResult:
    """The body of activate_from_payment (separated so the public wrapper can always roll back)."""
    known_row = (
        await db.execute(
            select(PaymentOrder.id, PaymentOrder.mode).where(PaymentOrder.razorpay_order_id == razorpay_order_id)
        )
    ).first()
    if known_row is None:
        raise ActivationError("order_not_found", "Unknown order.", http_status=404)
    known = known_row.id
    if known_row.mode != gateway.mode:
        # A live process must never turn a test order into a paid plan (and the reverse): refuse before any
        # network call or write. Test orders only exist because of test keys / the mock; real money never does.
        logger.error(
            "billing: order %s was created in %s mode but this process runs in %s mode — activation refused (%s)",
            known, known_row.mode, gateway.mode, source,
        )
        raise ActivationError(
            "mode_mismatch",
            f"This order belongs to {known_row.mode} mode but billing is running in {gateway.mode} mode.",
            http_status=409,
        )

    try:
        payment = await gateway.fetch_payment(razorpay_payment_id)
    except BillingGatewayError as exc:
        raise ActivationError(
            "gateway_error", "Could not confirm the payment with Razorpay. Please retry shortly.",
            http_status=502, retryable=True,
        ) from exc

    # Lock the order row; from here until commit no other activation of this order can proceed.
    order = (
        await db.execute(
            select(PaymentOrder).where(PaymentOrder.id == known)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()

    if order.status == PaymentOrderStatus.PAID:
        existing = await _subscription_for_order(db, order)
        if existing is None:
            raise ActivationError("invalid_state", "Order is paid but has no subscription.", http_status=500)
        await db.commit()  # nothing changed; ends the transaction and releases the row lock
        logger.info("billing: order %s already activated (%s) — idempotent no-op", order.id, source)
        return ActivationResult(subscription=existing, order=order, already_processed=True)
    if order.status == PaymentOrderStatus.REFUNDED:
        raise ActivationError("order_refunded", "This order was refunded.")

    _check_payment(payment, razorpay_order_id)

    paid_amount, paid_currency = payment.get("amount"), payment.get("currency", order.currency)
    if paid_amount != order.amount_paise or paid_currency != order.currency:
        order.status = PaymentOrderStatus.FAILED
        order.failure_reason = (
            f"amount_mismatch: gateway reports {paid_amount} {paid_currency}, "
            f"expected {order.amount_paise} {order.currency}"
        )
        await db.commit()
        logger.error("billing: AMOUNT MISMATCH on order %s (%s): %s", order.id, source, order.failure_reason)
        await _report_amount_mismatch(db, order.id, order.client_id, order.failure_reason or "", source)
        raise ActivationError("amount_mismatch", "The paid amount does not match the order amount.")

    try:
        sub = await _apply_purchase(db, order, now)
        order.status = PaymentOrderStatus.PAID
        order.razorpay_payment_id = razorpay_payment_id
        if signature:
            order.razorpay_signature = signature
        order.paid_at = now
        order.failure_reason = None
        await _issue_invoice(db, order, sub, now)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ActivationError(
            "payment_already_used", "This payment was already applied to another order.", http_status=409
        ) from exc

    logger.info(
        "billing: activated sub %s (%s, status=%s) for client %s from order %s via %s",
        sub.id, order.purpose, sub.status, order.client_id, order.id, source,
    )
    return ActivationResult(subscription=sub, order=order, already_processed=False)
