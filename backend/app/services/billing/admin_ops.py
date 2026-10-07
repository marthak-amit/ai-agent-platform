"""
Manual (admin) billing operations: grant a plan for an offline payment, extend or revoke a period, billing-exempt flag.

These bypass Razorpay, so every action is written to `billing_admin_log` (action, client, subscription, actor =
who, reason = why, plus detail) — the only audit trail for money-adjacent changes made by hand. A reason is
mandatory for every mutating call.

grant_subscription   a new purchased-style period, STACKED after whatever is running/queued (like a renewal), or
                     starting now when nothing is. Records "paid offline" as a PAID payment order: ₹0 by default
                     (receipt "offline_…", no invoice), or — when amount_paise (GST-inclusive, what was actually
                     received) is given — with that amount, its GST split and a tax invoice in the current series.
extend_subscription  push an active/pending period's end out by N days; later queued periods shift by the same
                     amount so periods never overlap.
revoke_subscription  cut an active/pending period short NOW (status 'revoked'); grace counts from this moment.
set_billing_exempt   flip clients.billing_exempt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingAdminLog,
    BillingPlan,
    ClientSubscription,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
    SubscriptionStatus,
)
from app.services.billing import invoices
from app.services.billing.subscriptions import (
    get_plan_by_code,
    is_intra_state,
    list_pending_subscriptions,
    now_utc,
    roll_forward,
    sync_legacy_client_plan,
)

logger = logging.getLogger(__name__)


class AdminOpError(Exception):
    """An admin operation was refused (`code` is machine-readable, `http_status` the status to answer with)."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        """Store the code, message and HTTP status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def log_admin_action(
    db: AsyncSession, action: str, *, actor: str = "admin-key", reason: str = "", client_id: int | None = None,
    subscription_id: int | None = None, payment_event_id: int | None = None, **detail: Any,
) -> None:
    """Add an audit row (who = *actor*, why = *reason*; the caller commits) and log it."""
    if reason:
        detail.setdefault("note", reason)       # older readers look for detail["note"]
    db.add(BillingAdminLog(
        action=action, actor=actor, reason=reason, client_id=client_id, subscription_id=subscription_id,
        payment_event_id=payment_event_id, detail=detail,
    ))
    logger.warning("billing ADMIN %s by=%s client=%s sub=%s reason=%r detail=%s", action, actor, client_id, subscription_id, reason, detail)


async def _lock_client(db: AsyncSession, client_id: int) -> Client:
    """The client row, locked FOR UPDATE (serialises concurrent admin actions on one client)."""
    client = (
        await db.execute(select(Client).where(Client.id == client_id).with_for_update())
    ).scalar_one_or_none()
    if client is None:
        raise AdminOpError("client_not_found", f"Unknown client {client_id}.", 404)
    return client


def _split_received(amount_paise: int, rate_bps: int, intra: bool) -> tuple[int, int, int, int, int]:
    """GST-inclusive *amount_paise* -> (taxable, gst, cgst, sgst, igst) at *rate_bps* (intra-state: CGST+SGST halves)."""
    gst = round(amount_paise * rate_bps / (10_000 + rate_bps))
    taxable = amount_paise - gst
    if intra:
        cgst = gst // 2
        return taxable, gst, cgst, gst - cgst, 0
    return taxable, gst, 0, 0, gst


async def grant_subscription(
    db: AsyncSession, *, client_id: int, plan_code: str, days: int | None, reason: str, actor: str = "admin-key",
    mode: str, amount_paise: int | None = None, settings: Settings | None = None, now: datetime | None = None,
) -> ClientSubscription:
    """
    Grant *days* (default: the plan's period) of *plan_code* to a client, stacked after existing periods, recorded
    as an offline payment: a PAID payment order of ₹0 — or of *amount_paise* with an invoice when that is given.
    """
    now = now or now_utc()
    settings = settings or get_settings()
    client = await _lock_client(db, client_id)
    plan: BillingPlan | None = await get_plan_by_code(db, plan_code, active_only=False)
    if plan is None:
        raise AdminOpError("unknown_plan", f"Unknown plan {plan_code!r}.", 404)
    if amount_paise is not None and amount_paise < 100:
        raise AdminOpError("invalid_amount", "amount_paise must be at least 100 (₹1), or omitted for a ₹0 grant.")

    active = await roll_forward(db, client_id, now)
    pending = await list_pending_subscriptions(db, client_id, for_update=True)
    ends = [now]
    if active is not None:
        ends.append(active.current_period_end)
    ends.extend(p.current_period_end for p in pending)
    start = max(ends)
    status = SubscriptionStatus.ACTIVE if start <= now else SubscriptionStatus.PENDING
    length = timedelta(days=days or plan.billing_period_days)

    token = secrets.token_hex(8)
    amount = amount_paise or 0
    taxable, gst, cgst, sgst, igst = _split_received(amount, settings.gst_rate_bps, is_intra_state(client, settings.seller_state_code))
    order = PaymentOrder(
        client_id=client_id, plan_id=plan.id, razorpay_order_id=f"offline_{token}", razorpay_payment_id=f"offline_pay_{token}",
        receipt=f"offline_{client_id}_{token}"[:40], amount_paise=amount, base_paise=taxable, gst_paise=gst,
        credit_paise=0, taxable_paise=taxable, cgst_paise=cgst, sgst_paise=sgst, igst_paise=igst,
        currency=plan.currency, status=PaymentOrderStatus.PAID, purpose=PaymentPurpose.NEW, mode=mode, paid_at=now,
        failure_reason=None,
    )
    db.add(order)
    await db.flush()

    sub = ClientSubscription(
        client_id=client_id, plan_id=plan.id, status=status,
        current_period_start=start, current_period_end=start + length,
        conversation_limit=plan.conversation_limit, conversations_used=0, credited_paise=0,
        source_payment_id=order.id,
    )
    db.add(sub)
    await db.flush()
    if status == SubscriptionStatus.ACTIVE:
        await sync_legacy_client_plan(db, client, plan, now)
    invoice_number = None
    if amount_paise:
        invoice = await invoices.create_invoice(db, order, client, plan, subscription_id=sub.id, now=now, settings=settings)
        invoice_number = invoice.invoice_number
    log_admin_action(
        db, "grant_subscription", actor=actor, reason=reason, client_id=client_id, subscription_id=sub.id,
        payment_order_id=order.id, plan=plan.code, days=int(length.days), status=status, mode=mode,
        amount_paise=amount, invoice=invoice_number,
        starts=start.isoformat(), ends=sub.current_period_end.isoformat(),
    )
    await db.commit()
    return sub


async def extend_subscription(
    db: AsyncSession, *, subscription_id: int, days: int, reason: str, actor: str = "admin-key",
    now: datetime | None = None,
) -> ClientSubscription:
    """Extend an active/pending period by *days*; periods queued after it move by the same amount."""
    now = now or now_utc()
    client_id = await db.scalar(select(ClientSubscription.client_id).where(ClientSubscription.id == subscription_id))
    if client_id is None:
        raise AdminOpError("subscription_not_found", f"Unknown subscription {subscription_id}.", 404)
    await _lock_client(db, client_id)
    sub = (
        await db.execute(
            select(ClientSubscription).where(ClientSubscription.id == subscription_id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING):
        raise AdminOpError(
            "not_extendable",
            f"Subscription {sub.id} is {sub.status}; grant a new period instead of extending it.", 409,
        )

    delta = timedelta(days=days)
    old_end = sub.current_period_end
    later = [
        p for p in await list_pending_subscriptions(db, client_id, for_update=True)
        if p.id != sub.id and p.current_period_start >= old_end
    ]
    sub.current_period_end = old_end + delta
    for p in later:
        p.current_period_start += delta
        p.current_period_end += delta
    log_admin_action(
        db, "extend_subscription", actor=actor, reason=reason, client_id=client_id, subscription_id=sub.id, days=days,
        old_end=old_end.isoformat(), new_end=sub.current_period_end.isoformat(),
        shifted_periods=[p.id for p in later],
    )
    await db.commit()
    return sub


async def revoke_subscription(
    db: AsyncSession, *, subscription_id: int, reason: str, actor: str = "admin-key", now: datetime | None = None
) -> ClientSubscription:
    """
    Revoke an active/pending period right now: status 'revoked', revoked_at = now. The tenant falls into the normal
    grace window (GRACE_DAYS, counted from now) unless they have another running period. Touches only this
    subscription (and the audit log); queued periods behind it are left as they are.
    """
    now = now or now_utc()
    client_id = await db.scalar(select(ClientSubscription.client_id).where(ClientSubscription.id == subscription_id))
    if client_id is None:
        raise AdminOpError("subscription_not_found", f"Unknown subscription {subscription_id}.", 404)
    await _lock_client(db, client_id)
    sub = (
        await db.execute(
            select(ClientSubscription).where(ClientSubscription.id == subscription_id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING):
        raise AdminOpError("not_revocable", f"Subscription {sub.id} is {sub.status}; only active or queued periods can be revoked.", 409)
    previous = sub.status
    sub.status = SubscriptionStatus.REVOKED
    sub.revoked_at = now
    log_admin_action(
        db, "revoke_subscription", actor=actor, reason=reason, client_id=client_id, subscription_id=sub.id,
        previous_status=previous, was_ending=sub.current_period_end.isoformat(),
    )
    await db.commit()
    return sub


async def set_billing_exempt(
    db: AsyncSession, *, client_id: int, exempt: bool, reason: str, actor: str = "admin-key"
) -> Client:
    """Set clients.billing_exempt (exempt clients are never restricted nor counted against a plan)."""
    client = await _lock_client(db, client_id)
    previous = bool(client.billing_exempt)
    client.billing_exempt = exempt
    log_admin_action(db, "set_billing_exempt", actor=actor, reason=reason, client_id=client_id, exempt=exempt, previous=previous)
    await db.commit()
    return client
