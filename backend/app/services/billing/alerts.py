"""
Dashboard billing alerts (the `billing_alerts` table — there is no general notifications table).

An alert is raised at most once per (client, dedupe_key): the key encodes WHAT happened and
for WHICH subscription/period (e.g. "usage_80:42"), so a new paid period can warn again while
a repeat of the same event never does. Raising is INSERT .. ON CONFLICT DO NOTHING, so it is
safe under concurrency and cheap to call speculatively.

`params` carries the numbers so the dashboard can render en/hi/gu text itself; `message` is the
English fallback (also what the email provider will use).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sellertalk24_billing import BillingAlert
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)

INFO, WARNING, CRITICAL = "info", "warning", "critical"


def build_alert(kind: str, **params: Any) -> tuple[str, str, str]:
    """
    Return (severity, title, English message) for an alert *kind* with its *params*.

    Kinds: usage_80 / usage_100 / usage_120 (pct, used, limit, plan), expiring_3d / expiring_1d
    (days, plan, end), grace_started (grace_days, grace_ends), expired, refund_revoked (plan, amount, grace_ends).
    """
    plan = params.get("plan", "your plan")
    if kind in ("usage_80", "usage_100", "usage_120"):
        pct, used, limit = params["pct"], params["used"], params["limit"]
        if pct == 80:
            return WARNING, "80% of conversations used", (
                f"You have used {used} of {limit} conversations on {plan} ({pct}%). "
                "Upgrade to keep room for more customers."
            )
        if pct == 100:
            return WARNING, "Conversation limit reached", (
                f"You have reached your {limit}-conversation limit on {plan}. "
                "Your assistant keeps working — upgrade soon to stay within your plan."
            )
        return CRITICAL, "Well over your conversation limit", (
            f"You are at {pct}% of your {plan} limit ({used}/{limit}). "
            "Your assistant keeps working, but please upgrade your plan."
        )
    if kind in ("expiring_3d", "expiring_1d"):
        days = params["days"]
        return (WARNING if days > 1 else CRITICAL), f"Plan expires in {days} day{'s' if days != 1 else ''}", (
            f"Your {plan} plan expires on {params['end']}. Renew now so your assistant is not interrupted."
        )
    if kind == "grace_started":
        return CRITICAL, "No active plan — grace period", (
            f"You have no active plan. Your assistant keeps working until {params['grace_ends']}; "
            "subscribe before then to avoid an interruption."
        )
    if kind == "refund_revoked":
        return CRITICAL, "Plan cancelled after refund", (
            f"Your {plan} payment of {params.get('amount', '')} was refunded, so the plan has been cancelled. "
            f"Your assistant keeps working until {params.get('grace_ends', 'the end of the grace period')}; "
            "subscribe again before then to avoid an interruption."
        )
    if kind == "expired":
        return CRITICAL, "Plan expired", (
            "Your plan has expired and the grace period is over. New customers now get a "
            "'temporarily unavailable' reply; orders already in progress still complete. Subscribe to restore the assistant."
        )
    raise ValueError(f"unknown alert kind {kind!r}")


async def raise_alert(
    db: AsyncSession,
    client_id: int,
    kind: str,
    dedupe_key: str,
    *,
    subscription_id: int | None = None,
    **params: Any,
) -> bool:
    """
    Create the alert unless (client, dedupe_key) already exists. Returns True if newly created.

    Does not commit (the caller owns the transaction).
    """
    severity, title, message = build_alert(kind, **params)
    created = await db.scalar(
        pg_insert(BillingAlert)
        .values(
            client_id=client_id, subscription_id=subscription_id, kind=kind, dedupe_key=dedupe_key,
            severity=severity, title=title, message=message, params=params,
        )
        .on_conflict_do_nothing(constraint="uq_sellertalk24_billing_alerts_dedupe")
        .returning(BillingAlert.id)
    )
    if created is not None:
        logger.info("billing alert raised: client=%s kind=%s key=%s", client_id, kind, dedupe_key)
    return created is not None


async def raise_admin_flag(
    db: AsyncSession, client_id: int, kind: str, dedupe_key: str, title: str, message: str,
    *, subscription_id: int | None = None, **params: Any,
) -> bool:
    """
    Record an INTERNAL flag for the operator (audience='admin'; never listed to the tenant), once per
    (client, dedupe_key). Returns True if newly created. Does not commit.
    """
    created = await db.scalar(
        pg_insert(BillingAlert)
        .values(
            client_id=client_id, subscription_id=subscription_id, kind=kind, dedupe_key=dedupe_key,
            severity=WARNING, title=title, message=message, params=params, audience="admin",
        )
        .on_conflict_do_nothing(constraint="uq_sellertalk24_billing_alerts_dedupe")
        .returning(BillingAlert.id)
    )
    if created is not None:
        logger.warning("billing ADMIN FLAG: client=%s kind=%s key=%s — %s", client_id, kind, dedupe_key, title)
    return created is not None


async def list_alerts(db: AsyncSession, client_id: int, *, unread_only: bool = False, limit: int = 50) -> list[BillingAlert]:
    """The client's own (tenant-audience) alerts, newest first."""
    stmt = select(BillingAlert).where(BillingAlert.client_id == client_id, BillingAlert.audience == "tenant")
    if unread_only:
        stmt = stmt.where(BillingAlert.read_at.is_(None))
    stmt = stmt.order_by(BillingAlert.created_at.desc(), BillingAlert.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def mark_read(db: AsyncSession, client_id: int, alert_id: int) -> bool:
    """Mark one of the client's alerts read. Returns False if it is not theirs / does not exist."""
    result = await db.execute(
        update(BillingAlert)
        .where(
            BillingAlert.id == alert_id, BillingAlert.client_id == client_id,
            BillingAlert.audience == "tenant", BillingAlert.read_at.is_(None),
        )
        .values(read_at=now_utc())
        .returning(BillingAlert.id)
    )
    found = result.scalar_one_or_none()
    if found is None:
        exists = await db.scalar(
            select(BillingAlert.id).where(
                BillingAlert.id == alert_id, BillingAlert.client_id == client_id, BillingAlert.audience == "tenant")
        )
        return exists is not None   # already read counts as success; foreign/unknown id does not
    return True
