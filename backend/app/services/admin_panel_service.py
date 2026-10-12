"""
Admin panel queries and operations that span all tenants.

Everything here is read-mostly and returns plain dicts shaped for app/schemas/admin_panel.py. Nothing in this
module ever returns a credential: channel tokens, Razorpay secrets, password hashes and bank details are
reported only as booleans ("is it set?").
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_user import AdminAuditLog
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.llm_usage import LLMUsage
from app.models.message import Message
from app.models.order import Order
from app.models.product import Product
from app.models.sellertalk24_billing import (
    BillingJobRun,
    BillingPlan,
    ClientSubscription,
    BillingMode,
    PaymentEvent,
    PaymentOrder,
    PaymentOrderStatus,
    SubscriptionStatus,
)
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services import admin_audit, admin_auth_service
from app.services.admin_audit import AdminPrincipal

logger = logging.getLogger(__name__)

IMPERSONATION_MINUTES = 30


def _now() -> datetime:
    """Current UTC time (patchable in tests)."""
    return datetime.now(timezone.utc)


def _like(q: str) -> str:
    """Escape LIKE wildcards in user input and wrap for a contains-match."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ── overview ─────────────────────────────────────────────────────────────────

async def get_overview(db: AsyncSession) -> dict[str, Any]:
    """Landing-page numbers: tenants, revenue, traffic, LLM spend, and the items that need a human."""
    now = _now()
    today = now.date()
    first_of_month = today.replace(day=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    two_weeks_ago = now - timedelta(days=13)

    total = int(await db.scalar(select(func.count(Client.id))) or 0)
    active = int(await db.scalar(select(func.count(Client.id)).where(Client.is_active.is_(True))) or 0)
    new_7d = int(await db.scalar(select(func.count(Client.id)).where(Client.created_at >= week_ago)) or 0)
    new_30d = int(await db.scalar(select(func.count(Client.id)).where(Client.created_at >= month_ago)) or 0)

    # Money actually collected: paid LIVE-mode payment orders (test/mock payments never count), GST-inclusive.
    collected_paise, paid_orders = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentOrder.amount_paise), 0), func.count(PaymentOrder.id)).where(
                PaymentOrder.status == PaymentOrderStatus.PAID,
                PaymentOrder.mode == BillingMode.LIVE,
                PaymentOrder.paid_at >= month_ago,
            )
        )
    ).one()
    active_subs = int(
        await db.scalar(
            select(func.count()).select_from(ClientSubscription).where(ClientSubscription.status == SubscriptionStatus.ACTIVE)
        )
        or 0
    )
    expiring = int(
        await db.scalar(
            select(func.count()).select_from(ClientSubscription).where(
                ClientSubscription.status == SubscriptionStatus.ACTIVE,
                ClientSubscription.current_period_end <= now + timedelta(days=7),
            )
        )
        or 0
    )
    over_limit = int(
        await db.scalar(
            select(func.count()).select_from(ClientSubscription).where(
                ClientSubscription.status == SubscriptionStatus.ACTIVE, ClientSubscription.over_limit.is_(True)
            )
        )
        or 0
    )

    msgs_today = int(await db.scalar(select(func.coalesce(func.sum(UsageLog.message_count), 0)).where(UsageLog.date == today)) or 0)
    msgs_month = int(
        await db.scalar(select(func.coalesce(func.sum(UsageLog.message_count), 0)).where(UsageLog.date >= first_of_month)) or 0
    )

    llm_row = (
        await db.execute(
            select(
                func.count(LLMUsage.id),
                func.coalesce(func.sum(LLMUsage.cost_inr), 0.0),
                func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0),
                func.count(LLMUsage.id).filter(LLMUsage.success.is_(False)),
            ).where(LLMUsage.created_at >= month_ago)
        )
    ).one()

    # Needs-a-human counters
    payment_review = int(await db.scalar(select(func.count(Order.id)).where(Order.status == "payment_submitted")) or 0)
    paused_bots = int(
        await db.scalar(
            select(func.count(Conversation.id)).where(Conversation.ai_enabled.is_(False), Conversation.is_sandbox.is_(False))
        )
        or 0
    )
    webhook_errors = int(
        await db.scalar(
            select(func.count()).select_from(PaymentEvent).where(
                PaymentEvent.error.is_not(None), PaymentEvent.processed.is_(False)
            )
        )
        or 0
    )

    signup_rows = (
        await db.execute(
            select(cast(Client.created_at, Date).label("d"), func.count(Client.id))
            .where(Client.created_at >= two_weeks_ago.replace(hour=0, minute=0, second=0, microsecond=0))
            .group_by("d")
            .order_by("d")
        )
    ).all()
    signup_map = {str(d): int(c) for d, c in signup_rows}
    llm_rows = (
        await db.execute(
            select(cast(LLMUsage.created_at, Date).label("d"), func.coalesce(func.sum(LLMUsage.cost_inr), 0.0), func.count(LLMUsage.id))
            .where(LLMUsage.created_at >= two_weeks_ago.replace(hour=0, minute=0, second=0, microsecond=0))
            .group_by("d")
            .order_by("d")
        )
    ).all()
    llm_map = {str(d): (float(cost), int(n)) for d, cost, n in llm_rows}
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]

    return {
        "clients": {"total": total, "active": active, "suspended": total - active, "new_7d": new_7d, "new_30d": new_30d},
        "revenue": {
            "collected_30d_inr": round(int(collected_paise) / 100, 2),
            "paid_orders_30d": int(paid_orders),
            "active_subscriptions": active_subs,
            "expiring_7d": expiring,
        },
        "messages": {"today": msgs_today, "this_month": msgs_month},
        "llm_30d": {
            "calls": int(llm_row[0]),
            "cost_inr": round(float(llm_row[1]), 2),
            "tokens": int(llm_row[2]),
            "failures": int(llm_row[3]),
        },
        "attention": {
            "payments_awaiting_review": payment_review,
            "bots_paused": paused_bots,
            "billing_webhook_errors": webhook_errors,
            "over_limit_clients": over_limit,
        },
        "signups_14d": [{"date": d, "count": signup_map.get(d, 0)} for d in days],
        "llm_daily_14d": [
            {"date": d, "cost_inr": round(llm_map.get(d, (0.0, 0))[0], 2), "calls": llm_map.get(d, (0.0, 0))[1]} for d in days
        ],
    }


# ── client directory ─────────────────────────────────────────────────────────

def _client_row(c: Client, sub: Optional[ClientSubscription], plan: Optional[BillingPlan]) -> dict[str, Any]:
    """Directory row for one client (credentials reported as booleans only)."""
    return {
        "id": c.id,
        "email": c.email,
        "business_name": c.business_name or "",
        "phone": c.phone,
        "is_active": c.is_active,
        "billing_exempt": bool(c.billing_exempt),
        "plan_slug": c.plan_slug or "starter",
        "sub_plan": plan.name if plan else None,
        "sub_status": sub.status if sub else None,
        "sub_period_end": sub.current_period_end if sub else None,
        "conversations_used": sub.conversations_used if sub else None,
        "conversation_limit": sub.conversation_limit if sub else None,
        "whatsapp_connected": bool(c.whatsapp_phone_number_id and c.whatsapp_access_token),
        "instagram_connected": bool(c.instagram_access_token),
        "created_at": c.created_at,
    }


async def search_clients(
    db: AsyncSession, *, q: str = "", status_filter: str = "", page: int = 1, page_size: int = 25
) -> dict[str, Any]:
    """
    Paginated client directory with each client's current (active) subscription.

    Args:
        q: matches id, email, business name or phone (case-insensitive contains).
        status_filter: "active" | "suspended" | "" (all).
    """
    filters = []
    q = (q or "").strip()
    if q:
        pattern = _like(q)
        conds = [Client.email.ilike(pattern, escape="\\"), Client.business_name.ilike(pattern, escape="\\"), Client.phone.ilike(pattern, escape="\\")]
        if q.isdigit():
            conds.append(Client.id == int(q))
        filters.append(or_(*conds))
    if status_filter == "active":
        filters.append(Client.is_active.is_(True))
    elif status_filter == "suspended":
        filters.append(Client.is_active.is_(False))

    total = int(await db.scalar(select(func.count(Client.id)).where(*filters)) or 0)
    clients = (
        await db.execute(
            select(Client).where(*filters).order_by(Client.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    subs: dict[int, tuple[ClientSubscription, BillingPlan]] = {}
    ids = [c.id for c in clients]
    if ids:
        rows = (
            await db.execute(
                select(ClientSubscription, BillingPlan)
                .join(BillingPlan, BillingPlan.id == ClientSubscription.plan_id)
                .where(ClientSubscription.client_id.in_(ids), ClientSubscription.status == SubscriptionStatus.ACTIVE)
            )
        ).all()
        subs = {s.client_id: (s, p) for s, p in rows}
    items = [_client_row(c, *(subs.get(c.id) or (None, None))) for c in clients]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_client(db: AsyncSession, client_id: int) -> Optional[Client]:
    """Load one client (None if it doesn't exist)."""
    return await db.get(Client, client_id)


async def get_client_detail(db: AsyncSession, client: Client) -> dict[str, Any]:
    """Full support view of one tenant — profile, flags, channel status, billing, team, counts, usage, LLM cost."""
    cid = client.id
    now = _now()
    today = now.date()
    first_of_month = today.replace(day=1)

    users = (await db.execute(select(User).where(User.client_id == cid).order_by(User.id))).scalars().all()

    active_sub_row = (
        await db.execute(
            select(ClientSubscription, BillingPlan)
            .join(BillingPlan, BillingPlan.id == ClientSubscription.plan_id)
            .where(ClientSubscription.client_id == cid, ClientSubscription.status == SubscriptionStatus.ACTIVE)
        )
    ).first()
    queued = int(
        await db.scalar(
            select(func.count()).select_from(ClientSubscription).where(
                ClientSubscription.client_id == cid, ClientSubscription.status == SubscriptionStatus.PENDING
            )
        )
        or 0
    )

    conv_count = int(await db.scalar(select(func.count(Conversation.id)).where(Conversation.client_id == cid, Conversation.is_sandbox.is_(False))) or 0)
    paused = int(
        await db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.client_id == cid, Conversation.ai_enabled.is_(False), Conversation.is_sandbox.is_(False)
            )
        )
        or 0
    )
    order_count = int(await db.scalar(select(func.count(Order.id)).where(Order.client_id == cid)) or 0)
    pending_pay = int(await db.scalar(select(func.count(Order.id)).where(Order.client_id == cid, Order.status == "payment_submitted")) or 0)
    product_count = int(await db.scalar(select(func.count(Product.id)).where(Product.client_id == cid)) or 0)

    msgs_today = int(await db.scalar(select(func.coalesce(func.sum(UsageLog.message_count), 0)).where(UsageLog.client_id == cid, UsageLog.date == today)) or 0)
    msgs_month = int(
        await db.scalar(
            select(func.coalesce(func.sum(UsageLog.message_count), 0)).where(UsageLog.client_id == cid, UsageLog.date >= first_of_month)
        )
        or 0
    )
    llm = (
        await db.execute(
            select(
                func.count(LLMUsage.id),
                func.coalesce(func.sum(LLMUsage.cost_inr), 0.0),
                func.count(LLMUsage.id).filter(LLMUsage.success.is_(False)),
            ).where(LLMUsage.client_id == cid, LLMUsage.created_at >= now - timedelta(days=30))
        )
    ).one()

    sub, plan = active_sub_row if active_sub_row else (None, None)
    return {
        "profile": {
            "id": cid,
            "email": client.email,
            "business_name": client.business_name or "",
            "phone": client.phone,
            "business_type": client.business_type,
            "whatsapp_number": client.whatsapp_number,
            "catalogue_slug": client.catalogue_slug,
            "gst_number": client.gst_number,
            "is_active": client.is_active,
            "onboarding_completed": bool(client.onboarding_completed),
            "created_at": client.created_at,
        },
        "flags": {
            "router_v2_enabled": client.router_v2_enabled,
            "billing_exempt": bool(client.billing_exempt),
            "plan_grandfathered": bool(client.plan_grandfathered),
            "daily_message_limit": client.daily_message_limit,
            "bot_auto_resume_minutes": client.bot_auto_resume_minutes,
            "plan_slug": client.plan_slug,
        },
        "channels": {
            "whatsapp_phone_number_id": client.whatsapp_phone_number_id,
            "whatsapp_token_set": bool(client.whatsapp_access_token),
            "instagram_account_id": client.instagram_account_id,
            "instagram_token_set": bool(client.instagram_access_token),
            "upi_configured": bool(client.upi_id),
            "razorpay_configured": bool(client.razorpay_key_id and client.razorpay_key_secret),
        },
        "billing": {
            "active_subscription": (
                {
                    "id": sub.id,
                    "plan_code": plan.code,
                    "plan_name": plan.name,
                    "status": sub.status,
                    "period_start": sub.current_period_start,
                    "period_end": sub.current_period_end,
                    "conversations_used": sub.conversations_used,
                    "conversation_limit": sub.conversation_limit,
                    "over_limit": bool(sub.over_limit),
                }
                if sub
                else None
            ),
            "queued_periods": queued,
        },
        "users": [
            {"id": u.id, "email": u.email, "role": u.role, "is_active": u.is_active, "created_at": u.created_at, "invited": u.hashed_password is None}
            for u in users
        ],
        "counts": {
            "conversations": conv_count,
            "paused_conversations": paused,
            "orders": order_count,
            "orders_awaiting_payment_review": pending_pay,
            "products": product_count,
        },
        "usage": {"messages_today": msgs_today, "messages_this_month": msgs_month},
        "llm_30d": {"calls": int(llm[0]), "cost_inr": round(float(llm[1]), 2), "failures": int(llm[2])},
    }


async def recent_client_audit(db: AsyncSession, client_id: int, limit: int = 10) -> list[AdminAuditLog]:
    """Latest audit rows that concern a client."""
    return list(
        (
            await db.execute(
                select(AdminAuditLog).where(AdminAuditLog.client_id == client_id).order_by(AdminAuditLog.id.desc()).limit(limit)
            )
        ).scalars()
    )


_FLAG_FIELDS = ("router_v2_enabled", "daily_message_limit", "bot_auto_resume_minutes", "plan_grandfathered")


async def update_client_flags(
    db: AsyncSession, client: Client, updates: dict[str, Any], principal: AdminPrincipal, reason: str
) -> dict[str, Any]:
    """
    Apply operational flag changes to a client and write one audit row with before/after.

    Only keys in `updates` (already limited to the explicitly-sent fields) change; NULL is meaningful for
    router_v2_enabled ("follow the env default"). Commits.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in _FLAG_FIELDS:
        if field not in updates:
            continue
        new_value = updates[field]
        if field != "router_v2_enabled" and new_value is None:
            continue
        old_value = getattr(client, field)
        if old_value != new_value:
            before[field] = old_value
            after[field] = new_value
            setattr(client, field, new_value)
    if after:
        admin_audit.record(
            db, principal, "client.flags", target_type="client", target_id=client.id, client_id=client.id,
            reason=reason, detail={"before": before, "after": after},
        )
        await db.commit()
    return {"changed": after}


async def issue_impersonation_token(
    db: AsyncSession, client: Client, principal: AdminPrincipal, reason: str
) -> tuple[str, str]:
    """
    Mint a short-lived tenant JWT for the client's owner login and audit it.

    Returns (token, owner_email). Raises ValueError if the client is suspended or has no owner login.
    """
    if not client.is_active:
        raise ValueError("Client is suspended; activate it before impersonating.")
    owner = (
        await db.execute(
            select(User).where(User.client_id == client.id, User.role == "owner", User.is_active.is_(True)).order_by(User.id).limit(1)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise ValueError("Client has no active owner login.")
    from app.services import auth_service

    token = auth_service.create_access_token(
        {"sub": owner.email, "impersonated_by": principal.actor},
        expires_delta=timedelta(minutes=IMPERSONATION_MINUTES),
    )
    admin_audit.record(
        db, principal, "client.impersonate", target_type="client", target_id=client.id, client_id=client.id,
        reason=reason, detail={"as": owner.email, "minutes": IMPERSONATION_MINUTES},
    )
    await db.commit()
    return token, owner.email


# ── conversations / orders ───────────────────────────────────────────────────

async def list_client_conversations(db: AsyncSession, client_id: int, *, page: int, page_size: int) -> dict[str, Any]:
    """A client's real (non-sandbox) conversations, most recently active first, with message counts."""
    base = (Conversation.client_id == client_id, Conversation.is_sandbox.is_(False))
    total = int(await db.scalar(select(func.count(Conversation.id)).where(*base)) or 0)
    msg_stats = (
        select(Message.conversation_id.label("cid"), func.count(Message.id).label("n"), func.max(Message.created_at).label("last"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Conversation, msg_stats.c.n, msg_stats.c.last)
            .outerjoin(msg_stats, msg_stats.c.cid == Conversation.id)
            .where(*base)
            .order_by(func.coalesce(msg_stats.c.last, Conversation.created_at).desc(), Conversation.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = [
        {
            "id": c.id,
            "phone_number": c.phone_number,
            "channel": c.channel,
            "current_stage": c.current_stage,
            "ai_enabled": c.ai_enabled,
            "bot_pause_source": c.bot_pause_source,
            "customer_name": c.customer_name,
            "is_sandbox": c.is_sandbox,
            "message_count": int(n or 0),
            "last_message_at": last,
            "created_at": c.created_at,
        }
        for c, n, last in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_conversation_messages(db: AsyncSession, client_id: int, conversation_id: int, limit: int = 200) -> Optional[list[Message]]:
    """The newest `limit` messages of a conversation, oldest first; None if it isn't this client's conversation."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.client_id != client_id:
        return None
    rows = (
        await db.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id.desc()).limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def list_client_orders(
    db: AsyncSession, client_id: int, *, status_filter: str = "", page: int, page_size: int
) -> dict[str, Any]:
    """A client's orders, newest first, optionally filtered by status."""
    filters = [Order.client_id == client_id]
    if status_filter:
        filters.append(Order.status == status_filter)
    total = int(await db.scalar(select(func.count(Order.id)).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(Order).where(*filters).order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    items = [
        {
            "id": o.id, "order_number": o.order_number, "customer_name": o.customer_name, "customer_phone": o.customer_phone,
            "product_name": o.product_name, "quantity": o.quantity, "total_amount": o.total_amount,
            "payment_method": o.payment_method, "payment_status": o.payment_status, "status": o.status,
            "created_at": o.created_at, "paid_at": o.paid_at,
        }
        for o in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── billing plans ────────────────────────────────────────────────────────────

async def list_billing_plans(db: AsyncSession) -> list[dict[str, Any]]:
    """Every sellable plan (active or not) with its number of active subscribers."""
    counts = dict(
        (
            await db.execute(
                select(ClientSubscription.plan_id, func.count())
                .where(ClientSubscription.status == SubscriptionStatus.ACTIVE)
                .group_by(ClientSubscription.plan_id)
            )
        ).all()
    )
    plans = (await db.execute(select(BillingPlan).order_by(BillingPlan.sort_order, BillingPlan.id))).scalars().all()
    return [_billing_plan_dict(p, int(counts.get(p.id, 0))) for p in plans]


def _billing_plan_dict(p: BillingPlan, subscribers: int) -> dict[str, Any]:
    """BillingPlan -> BillingPlanOut shape."""
    return {
        "id": p.id, "code": p.code, "name": p.name, "conversation_limit": p.conversation_limit,
        "price_paise": p.price_paise, "currency": p.currency, "billing_period_days": p.billing_period_days,
        "features": dict(p.features or {}), "is_active": p.is_active, "sort_order": p.sort_order,
        "active_subscribers": subscribers,
    }


async def update_billing_plan(
    db: AsyncSession, plan_id: int, updates: dict[str, Any], principal: AdminPrincipal, reason: str
) -> Optional[dict[str, Any]]:
    """
    Edit a sellable plan and audit before/after. Returns None for an unknown plan.

    Already-paid subscription periods keep their snapshotted conversation_limit, so edits only affect future purchases.
    """
    plan = await db.get(BillingPlan, plan_id)
    if plan is None:
        return None
    before = {k: getattr(plan, k) for k in updates}
    for key, value in updates.items():
        setattr(plan, key, value)
    admin_audit.record(
        db, principal, "billing_plan.update", target_type="billing_plan", target_id=plan_id, reason=reason,
        detail={"code": plan.code, "before": before, "after": updates},
    )
    await db.commit()
    await db.refresh(plan)
    subscribers = int(
        await db.scalar(
            select(func.count()).select_from(ClientSubscription).where(
                ClientSubscription.plan_id == plan_id, ClientSubscription.status == SubscriptionStatus.ACTIVE
            )
        )
        or 0
    )
    return _billing_plan_dict(plan, subscribers)


# ── operators ────────────────────────────────────────────────────────────────

def generate_temporary_password() -> str:
    """A random 16-char password that satisfies the operator password policy."""
    while True:
        candidate = secrets.token_urlsafe(12)
        try:
            admin_auth_service.validate_password_strength(candidate)
            return candidate
        except ValueError:
            continue


async def list_audit_log(
    db: AsyncSession, *, actor: str = "", action: str = "", client_id: Optional[int] = None,
    success: Optional[bool] = None, since: Optional[datetime] = None, until: Optional[datetime] = None,
    page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    """Filtered, paginated audit trail, newest first."""
    filters = []
    if actor:
        filters.append(AdminAuditLog.actor.ilike(_like(actor), escape="\\"))
    if action:
        filters.append(AdminAuditLog.action.ilike(_like(action), escape="\\"))
    if client_id is not None:
        filters.append(AdminAuditLog.client_id == client_id)
    if success is not None:
        filters.append(AdminAuditLog.success.is_(success))
    if since is not None:
        filters.append(AdminAuditLog.created_at >= since)
    if until is not None:
        filters.append(AdminAuditLog.created_at <= until)
    total = int(await db.scalar(select(func.count(AdminAuditLog.id)).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(AdminAuditLog).where(*filters).order_by(AdminAuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {"items": list(rows), "total": total, "page": page, "page_size": page_size}


# ── system health ────────────────────────────────────────────────────────────

async def get_system_health(db: AsyncSession) -> dict[str, Any]:
    """
    Live platform health for the operator: DB, LLM breaker/failures, Instagram verdicts, scheduler jobs,
    non-secret configuration, and the billing pipeline (webhook freshness, errors, job runs).
    """
    from sqlalchemy import text

    from app.config import get_settings
    from app.services import channel_status, llm_health

    settings = get_settings()
    now = _now()
    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # report, never raise: this page is for diagnosing outages
        checks["database"] = f"error: {type(exc).__name__}"

    llm = llm_health.health_summary()
    checks["llm"] = "ok" if llm["available"] else f"unavailable: {llm['reason']}"
    ig_bad = channel_status.instagram_invalid_clients()
    if channel_status.is_instagram_disabled():
        checks["instagram"] = "disconnected (kill switch)"
    elif ig_bad:
        checks["instagram"] = f"invalid token for client(s) {ig_bad}"
    else:
        checks["instagram"] = "ok"

    jobs: list[dict[str, Any]] = []
    try:
        from app.scheduler import scheduler

        for job in scheduler.get_jobs():
            nrt = getattr(job, "next_run_time", None)
            jobs.append({"id": job.id, "trigger": str(job.trigger), "next_run_time": nrt.isoformat() if nrt else None})
        checks["scheduler"] = "ok" if scheduler.running else "not running"
    except Exception as exc:
        checks["scheduler"] = f"error: {type(exc).__name__}"

    billing: dict[str, Any] = {}
    try:
        last_webhook = await db.scalar(select(func.max(PaymentEvent.created_at)))
        errors_24h = int(
            await db.scalar(
                select(func.count()).select_from(PaymentEvent).where(
                    PaymentEvent.error.is_not(None), PaymentEvent.created_at >= now - timedelta(hours=24)
                )
            )
            or 0
        )
        runs = (await db.execute(select(BillingJobRun))).scalars().all()
        billing = {
            "last_webhook_at": last_webhook,
            "webhook_errors_24h": errors_24h,
            "jobs": [
                {"name": getattr(r, "job", None) or getattr(r, "name", None) or str(getattr(r, "id", "")), "last_finished_at": r.last_finished_at, "status": r.last_status}
                for r in runs
            ],
        }
        if errors_24h:
            checks["billing_webhooks"] = f"{errors_24h} error(s) in 24h"
    except Exception as exc:
        billing = {"error": type(exc).__name__}

    router_ids = (settings.router_v2_client_ids or "").strip()
    config = {
        "environment": settings.environment,
        "router_v2_client_ids": router_ids or "(none)",
        "llm_models": {
            "reply": settings.llm_model_reply,
            "classifier": settings.llm_model_classifier,
            "vision": settings.llm_model_vision or "(disabled)",
            "stt": settings.llm_model_stt,
        },
        "admin_api_key_enabled": settings.admin_api_key_enabled,
        "secrets_configured": {
            "groq": bool(settings.groq_api_key),
            "gemini": bool(settings.gemini_api_key),
            "meta_app_secret": bool(settings.meta_app_secret),
            "razorpay": bool(settings.razorpay_key_id and settings.razorpay_key_secret),
        },
    }
    degraded = any(not (v == "ok" or v.startswith("ok")) for v in checks.values())
    return {
        "status": "degraded" if degraded else "healthy",
        "checks": checks,
        "llm": llm,
        "instagram": {"disabled": channel_status.is_instagram_disabled(), "client_verdicts": channel_status.instagram_client_status_summary(), "invalid_clients": ig_bad},
        "scheduler": jobs,
        "config": config,
        "billing": billing,
        "timestamp": now,
    }
