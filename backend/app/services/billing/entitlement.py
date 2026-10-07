"""
Entitlement: may this client's assistant answer NEW customers right now?

States (EntitlementState):
  EXEMPT   client.billing_exempt (e.g. the house account) — never restricted, never counted against a plan
  ACTIVE   a subscription period is running (active, or a queued period whose start has passed)
  GRACE    no running period, but within GRACE_DAYS of the last period's end (or of signup, for a
           client that never subscribed): the bot keeps working; the dashboard shows a red banner
  EXPIRED  no running period and the grace is over

Only EXPIRED restricts anything, and only when enforcement is switched on
(SELLERTALK24_BILLING_ENFORCE; off by default so deploying billing cannot silently shut down
existing clients that have not subscribed yet). Even then:
  * a customer mid-purchase (order in progress) is ALWAYS served to completion;
  * a human-takeover conversation is untouched (the bot is silent there anyway);
  * everything else gets one fixed, polite template — no LLM call.

This is read-only and cheap (<= 2 indexed queries) because it runs on every inbound message.
It deliberately does NOT use roll_forward (which writes + locks): a queued renewal whose start
has passed counts as running here even before the scheduler promotes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sellertalk24_billing import ClientSubscription, SubscriptionStatus
from app.services.billing.subscriptions import last_period_query, now_utc

logger = logging.getLogger(__name__)

#: Conversation stages with no purchase underway. Any other stage means an order is in progress.
IDLE_STAGES = frozenset({"greeting", "product_inquiry", "completed"})
#: Order statuses that mean "customer is waiting on a payment step".
OPEN_ORDER_STATUSES = ("pending_payment", "payment_submitted")


class EntitlementState(str, Enum):
    """See the module docstring."""

    EXEMPT = "exempt"
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Entitlement:
    """A client's billing standing at a moment in time."""

    state: EntitlementState
    subscription_id: int | None = None
    over_limit: bool = False
    grace_ends_at: datetime | None = None
    anchor_key: str = ""          # identifies WHICH lapse this is (for one-time alerts): last sub id or "never"

    @property
    def restricted(self) -> bool:
        """True when new customers would be refused IF enforcement is on."""
        return self.state is EntitlementState.EXPIRED


def enforcement_enabled() -> bool:
    """Whether EXPIRED actually restricts the bot (SELLERTALK24_BILLING_ENFORCE)."""
    return bool(get_settings().sellertalk24_billing_enforce)


def grace_days() -> int:
    """Length of the grace period in days (GRACE_DAYS)."""
    return int(get_settings().grace_days)


async def get_entitlement(db: AsyncSession, client, now: datetime | None = None) -> Entitlement:
    """
    Compute *client*'s entitlement at *now* (client is a loaded Client row).

    Queries (non-exempt only): the running period, then — if none — the last period end.
    """
    now = now or now_utc()
    if getattr(client, "billing_exempt", False):
        return Entitlement(EntitlementState.EXEMPT)

    running = (
        await db.execute(
            select(ClientSubscription.id, ClientSubscription.over_limit)
            .where(
                ClientSubscription.client_id == client.id,
                ClientSubscription.status.in_((SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING)),
                ClientSubscription.current_period_start <= now,
                ClientSubscription.current_period_end > now,
            )
            .order_by(ClientSubscription.current_period_start.desc())
            .limit(1)
        )
    ).first()
    if running is not None:
        return Entitlement(EntitlementState.ACTIVE, subscription_id=running.id, over_limit=bool(running.over_limit))

    last = (await db.execute(last_period_query(client.id))).first()
    if last is not None:
        anchor, anchor_key = last.current_period_end, str(last.id)
    else:
        anchor, anchor_key = getattr(client, "created_at", None) or now, "never"

    grace_ends = anchor + timedelta(days=grace_days())
    state = EntitlementState.GRACE if now < grace_ends else EntitlementState.EXPIRED
    return Entitlement(state, grace_ends_at=grace_ends, anchor_key=anchor_key)


def stage_in_progress(stage: str | None, cart_items) -> bool:
    """
    True when the conversation is mid-purchase: any stage outside IDLE_STAGES, or a non-empty cart.

    "greeting"/"product_inquiry"/"completed" with no cart is the draft-empty state; everything
    else (collecting details, confirming, awaiting payment, switch-confirm prompts) is in progress.
    """
    if cart_items:
        return True
    return (stage or "greeting") not in IDLE_STAGES


async def order_in_progress(db: AsyncSession, conv) -> bool:
    """
    Whether the conversation has a purchase underway: by stage/cart, or an order awaiting payment
    (the customer may be about to send a payment screenshot while the stage looks idle).
    """
    if stage_in_progress(getattr(conv, "current_stage", None), getattr(conv, "cart_items", None)):
        return True
    from app.models.order import Order

    open_order = await db.scalar(
        select(func.count()).select_from(Order).where(
            Order.conversation_id == conv.id, Order.status.in_(OPEN_ORDER_STATUSES)
        )
    )
    return bool(open_order)


@dataclass(frozen=True)
class InboundDecision:
    """What the pipeline should do with an inbound message: serve it, or send the fixed fallback."""

    allow: bool
    state: EntitlementState
    reason: str = ""


async def decide_inbound(db: AsyncSession, client, conv, now: datetime | None = None) -> InboundDecision:
    """
    Gate for one inbound message (called before the pipeline runs).

    Serve unless: enforcement is on AND the client is EXPIRED AND the bot is actually the one
    replying (not paused for a human) AND no order is in progress. Fails OPEN on any error.
    """
    try:
        if not enforcement_enabled():
            return InboundDecision(True, EntitlementState.ACTIVE, "enforcement_off")
        ent = await get_entitlement(db, client, now)
        if not ent.restricted:
            return InboundDecision(True, ent.state)
        if getattr(conv, "ai_enabled", True) is False:
            return InboundDecision(True, ent.state, "human_takeover")
        if await order_in_progress(db, conv):
            return InboundDecision(True, ent.state, "order_in_progress")
        return InboundDecision(False, ent.state, "subscription_expired")
    except Exception:
        logger.exception("billing: entitlement check failed — failing OPEN (message is served)")
        return InboundDecision(True, EntitlementState.ACTIVE, "error_fail_open")


async def automation_allowed(db: AsyncSession, client_id: int, now: datetime | None = None) -> bool:
    """
    May automated, business-initiated sends (nudges, follow-ups, campaigns) go out for this client?

    True unless enforcement is on and the client is EXPIRED. Fails open. Used by the send gate.
    """
    try:
        if not enforcement_enabled():
            return True
        from app.models.client import Client

        client = await db.get(Client, client_id)
        if client is None:
            return True
        return not (await get_entitlement(db, client, now)).restricted
    except Exception:
        logger.exception("billing: automation entitlement check failed — failing OPEN")
        return True
