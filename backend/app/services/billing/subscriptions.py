"""
Subscription state helpers for SellerTalk24 billing.

Everything here is DB-derived (no in-memory billing state — the app runs several
workers). Functions take the caller's session and never commit; the caller owns the
transaction.

Lifecycle (client_subscriptions.status):
  active      the one running period (partial unique index: max one per client)
  pending     a paid period queued to start when the previous one ends (renewal stacking)
  expired     period ended
  superseded  replaced mid-cycle by an upgrade
  cancelled   reserved
  revoked     cut short by a full refund / an admin; the grace period counts from revoked_at

`roll_forward` is the lazy state transition (expire the lapsed active period, promote
the next pending one). It is idempotent and is called before every read/write that needs
an accurate "current" subscription, so correctness never depends on a scheduler tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.plan import Plan as LegacyPlan
from app.models.sellertalk24_billing import (
    BillingPlan,
    ClientSubscription,
    PaymentOrder,
    PaymentPurpose,
    SubscriptionStatus,
)
from app.services.billing.pricing import (
    AmountBreakdown,
    compute_amounts,
    compute_upgrade_credit,
)

logger = logging.getLogger(__name__)

_GSTIN_STATE_RE = re.compile(r"^(\d{2})[0-9A-Z]{13}$")

# billing_plans.code -> legacy plans.plan_id (the pre-Razorpay tier slug on Client.plan_slug)
LEGACY_PLAN_SLUG = {"starter_1500": "starter", "growth_5000": "growth", "pro_9000": "pro"}


class CheckoutError(Exception):
    """A checkout/billing request was refused; carries an HTTP status and a client-safe message."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        """Store the machine code, the human message and the HTTP status to answer with."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class Quote:
    """What a checkout for one plan would cost this client right now."""

    plan: BillingPlan
    purpose: str
    breakdown: AmountBreakdown
    active: ClientSubscription | None
    active_plan: BillingPlan | None


def now_utc() -> datetime:
    """Current time, tz-aware UTC (a function so tests can inject their own clock)."""
    return datetime.now(timezone.utc)


# --- lookups -----------------------------------------------------------------

async def get_plan_by_code(db: AsyncSession, code: str, *, active_only: bool = True) -> BillingPlan | None:
    """Return the billing plan with *code* (only active plans unless active_only=False)."""
    stmt = select(BillingPlan).where(BillingPlan.code == code)
    if active_only:
        stmt = stmt.where(BillingPlan.is_active.is_(True))
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_plan_by_id(db: AsyncSession, plan_id: int) -> BillingPlan | None:
    """Return the billing plan row with this id (active or not)."""
    return await db.get(BillingPlan, plan_id)


async def list_active_plans(db: AsyncSession) -> list[BillingPlan]:
    """Active plans in display order."""
    result = await db.execute(
        select(BillingPlan).where(BillingPlan.is_active.is_(True)).order_by(BillingPlan.sort_order, BillingPlan.id)
    )
    return list(result.scalars().all())


async def get_active_subscription(
    db: AsyncSession, client_id: int, *, for_update: bool = False
) -> ClientSubscription | None:
    """The client's single status='active' subscription row, if any."""
    stmt = select(ClientSubscription).where(
        ClientSubscription.client_id == client_id,
        ClientSubscription.status == SubscriptionStatus.ACTIVE,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_pending_subscriptions(
    db: AsyncSession, client_id: int, *, for_update: bool = False
) -> list[ClientSubscription]:
    """Queued (stacked) periods, earliest first."""
    stmt = (
        select(ClientSubscription)
        .where(
            ClientSubscription.client_id == client_id,
            ClientSubscription.status == SubscriptionStatus.PENDING,
        )
        .order_by(ClientSubscription.current_period_start, ClientSubscription.id)
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


def lapse_anchor_expr():
    """
    SQL expression: when a non-pending period stopped counting. Its end — except a revoked one, whose end was
    cut short at revoked_at (its original current_period_end may still be in the future).
    """
    return case(
        (
            ClientSubscription.status == SubscriptionStatus.REVOKED,
            func.coalesce(ClientSubscription.revoked_at, ClientSubscription.current_period_end),
        ),
        else_=ClientSubscription.current_period_end,
    )


def last_period_query(client_id: int):
    """
    SELECT (id, current_period_end) of the client's most recently ended non-pending period, where
    `current_period_end` is the lapse anchor (see lapse_anchor_expr). Grace and renewal-vs-new both hang off it.
    """
    return (
        select(ClientSubscription.id, lapse_anchor_expr().label("current_period_end"))
        .where(ClientSubscription.client_id == client_id, ClientSubscription.status != SubscriptionStatus.PENDING)
        .order_by(lapse_anchor_expr().desc())
        .limit(1)
    )


async def source_order_base_paise(db: AsyncSession, sub: ClientSubscription) -> int:
    """
    What a subscription period is worth, in paise: the plan list price (base_paise) of the
    payment that bought it. base = taxable + credit applied (exclusive) or gross (inclusive),
    so it is the full value of the period regardless of any upgrade credit used to pay for it.
    A period with no source payment (e.g. granted manually) is worth 0 for credit purposes.
    """
    if sub.source_payment_id is None:
        return 0
    base = await db.scalar(select(PaymentOrder.base_paise).where(PaymentOrder.id == sub.source_payment_id))
    return int(base or 0)


# --- tax ---------------------------------------------------------------------

def is_intra_state(client: Client, seller_state_code: str) -> bool:
    """
    True when the supply is intra-state (CGST+SGST), False for inter-state (IGST).

    The buyer's state comes from the first two digits of their GSTIN. No (or malformed)
    GSTIN means no known state, which defaults to intra-state (the seller's own state).
    """
    gstin = (getattr(client, "gst_number", None) or "").strip().upper()
    match = _GSTIN_STATE_RE.match(gstin)
    if not match:
        return True
    return match.group(1) == seller_state_code


# --- lazy state transitions ------------------------------------------------------

async def roll_forward(db: AsyncSession, client_id: int, now: datetime | None = None) -> ClientSubscription | None:
    """
    Bring the client's subscription rows up to date at *now* and return the active one.

    * an active period whose end has passed becomes 'expired';
    * with no active period, the earliest pending period that has started is promoted to
      'active' (pending periods that already ended are expired);
    * a promotion re-syncs the legacy Client plan columns.

    Idempotent; locks the affected rows; never commits.
    """
    now = now or now_utc()
    active = await get_active_subscription(db, client_id, for_update=True)
    if active is not None and active.current_period_end <= now:
        active.status = SubscriptionStatus.EXPIRED
        await db.flush()
        active = None

    if active is None:
        for pending in await list_pending_subscriptions(db, client_id, for_update=True):
            if pending.current_period_start > now:
                break
            if pending.current_period_end <= now:
                pending.status = SubscriptionStatus.EXPIRED
                continue
            pending.status = SubscriptionStatus.ACTIVE
            await db.flush()
            active = pending
            plan = await get_plan_by_id(db, pending.plan_id)
            client = await db.get(Client, client_id)
            if plan is not None and client is not None:
                await sync_legacy_client_plan(db, client, plan, now)
            break
        await db.flush()
    return active


async def sync_legacy_client_plan(db: AsyncSession, client: Client, plan: BillingPlan, now: datetime) -> None:
    """
    Keep the pre-Razorpay Client columns (plan_slug + snapshots) in step with the paid plan.

    Legacy consumers (admin revenue, the older conversation counter) still read these.
    plan_grandfathered is set so billing_service.ensure_current_cycle does not overwrite the
    new conversation limit with the legacy `plans` table's old numbers on its next rollover.
    The legacy price snapshot is whole rupees (GST-exclusive).
    """
    slug = LEGACY_PLAN_SLUG.get(plan.code)
    legacy = await db.get(LegacyPlan, slug) if slug else None
    if legacy is not None:
        client.plan_slug = legacy.plan_id
        client.daily_message_limit = legacy.daily_msg_limit
        client.plan_image_quota_snapshot = legacy.image_quota
        client.plan_image_overage_price_snapshot = legacy.image_overage_price
    client.plan_conv_limit_snapshot = plan.conversation_limit
    client.plan_price_snapshot = plan.price_paise // 100
    client.billing_cycle_start = now.date()
    client.plan_grandfathered = True
    client.conv_limit_warned_period = None


# --- quoting -----------------------------------------------------------------

async def upgrade_credit_paise(
    db: AsyncSession, active: ClientSubscription, pending: list[ClientSubscription], now: datetime
) -> int:
    """
    Credit for switching plans mid-cycle: the pro-rata unused value of the active period plus
    the full value of any queued (already paid, not started) periods, which an upgrade replaces.
    """
    credit = compute_upgrade_credit(active, now, paid_paise=await source_order_base_paise(db, active))
    for queued in pending:
        credit += await source_order_base_paise(db, queued)
    return credit


async def build_quote(
    db: AsyncSession, client: Client, plan: BillingPlan, seller_state_code: str, now: datetime | None = None,
    *, grace_days: int | None = None,
) -> Quote:
    """
    Decide the purpose (new / renewal / upgrade) and the amounts for *client* buying *plan*.

    With no running period the purchase is a RENEWAL when the last period lapsed within *grace_days* (the customer
    is coming back inside the grace window) and NEW once grace is over — or if they never subscribed. Pass
    grace_days=None to skip that look-back (always NEW). Assumes roll_forward has been applied. Raises CheckoutError
    (400) for a mid-cycle downgrade.
    """
    now = now or now_utc()
    intra = is_intra_state(client, seller_state_code)
    active = await get_active_subscription(db, client.id)
    active_plan = await get_plan_by_id(db, active.plan_id) if active is not None else None

    if active is None:
        purpose, credit = PaymentPurpose.NEW, 0
        if grace_days is not None:
            last = (await db.execute(last_period_query(client.id))).first()
            if last is not None and now < last.current_period_end + timedelta(days=grace_days):
                purpose = PaymentPurpose.RENEWAL
    elif plan.id == active.plan_id:
        purpose, credit = PaymentPurpose.RENEWAL, 0
    elif plan.sort_order > active_plan.sort_order:
        purpose = PaymentPurpose.UPGRADE
        credit = await upgrade_credit_paise(db, active, await list_pending_subscriptions(db, client.id), now)
    else:
        raise CheckoutError(
            "downgrade_not_allowed",
            f"You are on {active_plan.name} until {active.current_period_end:%d %b %Y}. "
            f"Plan downgrades take effect after your current period ends — "
            f"you can switch to {plan.name} then.",
        )

    breakdown = compute_amounts(plan, credit, intra_state=intra)
    return Quote(plan=plan, purpose=purpose, breakdown=breakdown, active=active, active_plan=active_plan)


def days_left(sub: ClientSubscription, now: datetime) -> int:
    """Whole days remaining in the period, rounded up (the last partial day counts), never negative."""
    seconds = (sub.current_period_end - now).total_seconds()
    return max(0, ceil(seconds / 86400))


def percent_used(sub: ClientSubscription) -> float:
    """Conversations used as a percentage of the limit (may exceed 100 — the cap is soft)."""
    if sub.conversation_limit <= 0:
        return 0.0
    return round(sub.conversations_used * 100 / sub.conversation_limit, 1)


def plan_features(plan: BillingPlan) -> dict[str, Any]:
    """The plan's feature-flag object as a plain dict."""
    return dict(plan.features or {})
