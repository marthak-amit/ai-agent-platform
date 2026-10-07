"""
Razorpay webhook processing for SellerTalk24 billing.

Pipeline (the route is unauthenticated — the signature IS the authentication):

  1. verify X-Razorpay-Signature over the exact raw bytes      -> InvalidWebhookSignature
  2. persist the event in payment_events (UNIQUE razorpay_event_id; ON CONFLICT DO NOTHING)
     BEFORE doing any work, so the audit row exists even if handling blows up
  3. a replayed event that was already processed is a no-op     -> "duplicate"
  4. dispatch by event type; any failure is stored on the event row (payment_events.error)
     and never raised — the route answers HTTP 200 once the event is persisted.

Handled: payment.captured / order.paid (activate), payment.failed (mark failed with reason —
not terminal, the customer may retry the same order), refund.processed (credit note; a FULL refund revokes the
subscription and starts grace, a partial one only raises an admin flag). Everything else is acknowledged.

An event whose handling failed with a RETRYABLE error is left processed=false so a later
re-delivery (or a reprocessing job) handles it; a duplicate of such an event is re-processed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sellertalk24_billing import (
    BillingPlan,
    ClientSubscription,
    PaymentEvent,
    PaymentOrder,
    PaymentOrderStatus,
    SubscriptionStatus,
)
from app.services.billing import alerts, credit_notes
from app.services.billing.activation import ActivationError, activate_from_payment
from app.services.billing.entitlement import grace_days
from app.services.billing.invoice_pdf import rupees
from app.services.billing.razorpay_client import BillingGateway
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)

_ACTIVATING_EVENTS = {"payment.captured", "order.paid"}


class InvalidWebhookSignature(Exception):
    """The X-Razorpay-Signature header did not match the body."""


class InvalidWebhookPayload(Exception):
    """The body is not a JSON object (checked only after the signature passed)."""


@dataclass(frozen=True)
class WebhookOutcome:
    """What the route reports: 'ok' | 'duplicate' | 'ignored' | 'error'."""

    status: str


def _entity(payload: dict, kind: str) -> dict[str, Any]:
    """payload["payload"][kind]["entity"] or {} — Razorpay nests every event this way."""
    return (((payload.get("payload") or {}).get(kind) or {}).get("entity")) or {}


def fallback_event_id(raw_body: bytes) -> str:
    """Dedupe key when Razorpay's event-id header is missing: a hash of the exact body."""
    return "body-sha256:" + hashlib.sha256(raw_body).hexdigest()


async def _lock_order(db: AsyncSession, *, razorpay_order_id: str | None = None,
                      razorpay_payment_id: str | None = None) -> PaymentOrder | None:
    """Fetch an order by Razorpay order id or payment id, row-locked."""
    stmt = select(PaymentOrder).with_for_update().execution_options(populate_existing=True)
    if razorpay_order_id:
        stmt = stmt.where(PaymentOrder.razorpay_order_id == razorpay_order_id)
    elif razorpay_payment_id:
        stmt = stmt.where(PaymentOrder.razorpay_payment_id == razorpay_payment_id)
    else:
        return None
    return (await db.execute(stmt)).scalar_one_or_none()


async def _handle_activation(db: AsyncSession, gateway: BillingGateway, event_type: str,
                             payload: dict) -> tuple[str, str | None, bool]:
    """payment.captured / order.paid -> activate_from_payment. Returns (status, error, processed)."""
    payment = _entity(payload, "payment")
    order_id = payment.get("order_id") or _entity(payload, "order").get("id")
    payment_id = payment.get("id")
    if not order_id or not payment_id:
        return "error", f"{event_type}: payload has no order id / payment id", True

    try:
        await activate_from_payment(
            db, gateway, razorpay_order_id=order_id, razorpay_payment_id=payment_id, source="webhook"
        )
    except ActivationError as exc:
        logger.warning("billing webhook: %s for order %s refused: %s", event_type, order_id, exc.code)
        if exc.code == "order_not_found":
            return "ignored", f"order_not_found: {order_id} (not a SellerTalk24 order)", True
        return "error", f"{exc.code}: {exc.message}", not exc.retryable

    await db.execute(update(PaymentOrder).where(PaymentOrder.razorpay_order_id == order_id).values(raw_webhook=payload))
    await db.commit()
    return "ok", None, True


async def _handle_failed(db: AsyncSession, payload: dict) -> tuple[str, str | None, bool]:
    """payment.failed -> mark the order failed with Razorpay's reason (never overrides 'paid')."""
    payment = _entity(payload, "payment")
    order = await _lock_order(db, razorpay_order_id=payment.get("order_id"))
    if order is None:
        await db.rollback()
        return "ignored", f"order_not_found: {payment.get('order_id')}", True
    if order.status == PaymentOrderStatus.PAID:
        await db.rollback()
        return "ignored", "payment.failed for an already paid order", True

    reason = (
        payment.get("error_description") or payment.get("error_reason")
        or payment.get("error_code") or "payment failed"
    )
    first_failure = order.status != PaymentOrderStatus.FAILED
    order_pk = order.id
    order.status = PaymentOrderStatus.FAILED
    order.failure_reason = str(reason)[:500]
    order.raw_webhook = payload
    await db.commit()
    if first_failure:  # a customer retrying a failing card must not get one email per attempt
        from app.services.billing import emails

        await emails.notify_payment_failed(db, order_pk)
    return "ok", None, True


async def _handle_refund(db: AsyncSession, payload: dict, now: datetime) -> tuple[str, str | None, bool]:
    """
    refund.processed -> credit note, and (for a full refund) revoke the plan.

    One credit note per Razorpay refund id (a redelivery — even under a new event id — changes nothing).
    * Full refund (this refund brings the total refunded up to the order amount): order -> 'refunded', the
      subscription it bought is REVOKED immediately (active/pending only), the tenant gets an alert + email and
      enters grace measured from the revocation (the usual GRACE_DAYS).
    * Partial refund: credit note only; order stays paid, subscription untouched, and an internal admin flag
      (billing_alerts, audience='admin') asks a human to look at it.
    Everything is one transaction under the order's row lock.
    """
    refund = _entity(payload, "refund")
    payment_id = refund.get("payment_id") or _entity(payload, "payment").get("id")
    amount = refund.get("amount")
    refund_id = refund.get("id") or f"unidentified:{payment_id}:{amount}"
    order = await _lock_order(db, razorpay_payment_id=payment_id)
    if order is None:
        await db.rollback()
        return "ignored", f"unknown payment for refund: {payment_id}", True
    if not isinstance(amount, int) or amount <= 0:
        await db.rollback()
        return "error", f"refund.processed: payload has no usable amount ({amount!r})", True

    if await credit_notes.get_credit_note_for_refund(db, refund_id) is not None:
        await db.rollback()
        logger.info("billing: refund %s already has a credit note — redelivery ignored", refund_id)
        return "ok", None, True

    already = await credit_notes.refunded_total(db, order.id)
    sub = (
        await db.execute(
            select(ClientSubscription).where(ClientSubscription.source_payment_id == order.id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    order_pk, client_id = order.id, order.client_id

    if order.status != PaymentOrderStatus.PAID:
        # Refunded before we ever activated it (webhook still pending / order failed): block any later activation,
        # but there was no sale, so no credit note.
        if order.status != PaymentOrderStatus.REFUNDED:
            order.status = PaymentOrderStatus.REFUNDED
            order.raw_webhook = payload
            await db.commit()
            return "ok", f"refund for an order that was never activated (status was not paid): order {order_pk} blocked", True
        await db.rollback()
        return "ignored", "refund for an already refunded order", True

    remaining = order.amount_paise - already
    if remaining <= 0:
        await db.rollback()
        return "ignored", f"refund {refund_id} exceeds what is left to refund on order {order_pk}", True
    amount = min(amount, remaining)
    full = already + amount >= order.amount_paise
    note, _ = await credit_notes.issue_credit_note(
        db, order, razorpay_refund_id=refund_id, amount_paise=amount, kind="full" if full else "partial",
        reason="razorpay refund.processed", now=now,
    )
    credit_note_number = note.credit_note_number
    order.raw_webhook = payload

    if not full:
        sub_id = sub.id if sub else None
        await alerts.raise_admin_flag(
            db, client_id, "partial_refund", f"partial_refund:{refund_id}",
            "Partial refund — review",
            f"Order {order_pk} (client {client_id}) was partially refunded: {amount} of {order.amount_paise} paise "
            f"(credit note {credit_note_number}). The order stays paid and the subscription is untouched — "
            "decide whether to shorten/revoke it (POST /admin/billing/subscriptions/{id}/revoke).",
            subscription_id=sub_id, order_id=order_pk, refund_id=refund_id, amount_paise=amount,
            credit_note=credit_note_number,
        )
        await db.commit()
        logger.warning(
            "billing: PARTIAL REFUND of %s paise on order %s (client %s, sub %s) — credit note %s, review manually",
            amount, order_pk, client_id, sub_id, credit_note_number,
        )
        return "ok", None, True

    order.status = PaymentOrderStatus.REFUNDED
    revoked_sub_id = None
    if sub is not None and sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING):
        sub.status = SubscriptionStatus.REVOKED
        sub.revoked_at = now
        revoked_sub_id = sub.id
    plan_name = amount_text = ""
    if revoked_sub_id is not None:
        plan = await db.get(BillingPlan, order.plan_id)
        plan_name, amount_text = (plan.name if plan else "your plan"), rupees(order.amount_paise)
    created_alert = False
    if revoked_sub_id is not None:
        grace_ends = now + timedelta(days=grace_days())
        params = {"plan": plan_name, "amount": amount_text, "grace_ends": f"{grace_ends:%d %b %Y}"}
        created_alert = await alerts.raise_alert(
            db, client_id, "refund_revoked", f"refund_revoked:{revoked_sub_id}",
            subscription_id=revoked_sub_id, **params,
        )
    await db.commit()
    logger.error(
        "billing: FULL REFUND of order %s (client %s, payment %s) — credit note %s; subscription %s %s",
        order_pk, client_id, payment_id, credit_note_number, sub.id if sub else None,
        "REVOKED, tenant is now in grace" if revoked_sub_id is not None else "was not running (left as is)",
    )
    if created_alert:
        from app.services.billing import emails

        await emails.notify_alert(db, client_id, "refund_revoked", params)
    return "ok", None, True


async def _dispatch(db: AsyncSession, gateway: BillingGateway, event_type: str,
                    payload: dict, now: datetime) -> tuple[str, str | None, bool]:
    """Route one event to its handler; returns (outcome, error text or None, processed flag)."""
    if event_type in _ACTIVATING_EVENTS:
        return await _handle_activation(db, gateway, event_type, payload)
    if event_type == "payment.failed":
        return await _handle_failed(db, payload)
    if event_type == "refund.processed":
        return await _handle_refund(db, payload, now)
    return "ignored", None, True


async def process_webhook(
    db: AsyncSession,
    gateway: BillingGateway,
    raw_body: bytes,
    signature: str | None,
    event_id_header: str | None,
    now: datetime | None = None,
) -> WebhookOutcome:
    """
    Verify, persist, deduplicate and handle one Razorpay webhook delivery.

    Raises:
        InvalidWebhookSignature: bad/missing signature (nothing is persisted).
        InvalidWebhookPayload:   signature fine but the body is not a JSON object.
    """
    if not gateway.verify_webhook_signature(raw_body, signature or ""):
        raise InvalidWebhookSignature()
    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise InvalidWebhookPayload() from exc
    if not isinstance(payload, dict):
        raise InvalidWebhookPayload()

    now = now or now_utc()
    event_id = (event_id_header or "").strip() or fallback_event_id(raw_body)
    event_type = str(payload.get("event") or "unknown")

    inserted = await db.scalar(
        pg_insert(PaymentEvent)
        .values(razorpay_event_id=event_id, event_type=event_type, payload=payload)
        .on_conflict_do_nothing(index_elements=["razorpay_event_id"])
        .returning(PaymentEvent.id)
    )
    await db.commit()

    if inserted is not None:
        event_pk = inserted
    else:
        existing = (
            await db.execute(select(PaymentEvent.id, PaymentEvent.processed).where(PaymentEvent.razorpay_event_id == event_id))
        ).one()
        if existing.processed:
            logger.info("billing webhook: duplicate event %s (%s) ignored", event_id, event_type)
            return WebhookOutcome("duplicate")
        event_pk = existing.id  # earlier attempt failed retryably — handle it again

    outcome, _, _ = await _run_and_record(db, gateway, event_pk, event_id, event_type, payload, now)
    return WebhookOutcome(outcome)


async def _run_and_record(
    db: AsyncSession, gateway: BillingGateway, event_pk: int, event_id: str, event_type: str,
    payload: dict, now: datetime,
) -> tuple[str, str | None, bool]:
    """Dispatch one event and store the result on its payment_events row. Never raises (handler bugs are recorded)."""
    try:
        outcome, error, processed = await _dispatch(db, gateway, event_type, payload, now)
    except Exception as exc:  # never let a handler bug turn into a 500: persist and answer 200
        await db.rollback()
        logger.exception("billing webhook: handler crashed for event %s (%s)", event_id, event_type)
        outcome, error, processed = "error", f"unhandled {type(exc).__name__}: {exc}"[:1000], False

    await db.execute(
        update(PaymentEvent).where(PaymentEvent.id == event_pk).values(
            processed=processed, processed_at=now if processed else None, error=error
        )
    )
    await db.commit()
    return outcome, error, processed


class EventNotFound(Exception):
    """No payment_events row with that id."""


class EventNotReprocessable(Exception):
    """The event was processed cleanly; there is nothing to redo."""


async def reprocess_event(
    db: AsyncSession, gateway: BillingGateway, event_pk: int, now: datetime | None = None
) -> tuple[str, str | None, bool]:
    """
    Re-run a stored webhook event that failed (error set) or was never finished (processed=False).

    Safe by construction: activation, failure and refund handlers are idempotent. Returns
    (outcome, error, processed) exactly as the live path would have recorded it.

    Raises:
        EventNotFound:         unknown id.
        EventNotReprocessable: processed with no error.
    """
    event = (await db.execute(select(PaymentEvent).where(PaymentEvent.id == event_pk))).scalar_one_or_none()
    if event is None:
        raise EventNotFound(event_pk)
    if event.processed and not event.error:
        raise EventNotReprocessable(event_pk)
    return await _run_and_record(
        db, gateway, event.id, event.razorpay_event_id, event.event_type, event.payload, now or now_utc()
    )
