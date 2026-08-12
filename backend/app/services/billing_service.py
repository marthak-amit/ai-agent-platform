"""
Billing service — monthly billing-cycle rollover, conversation-count usage
tracking against a client's plan conv_limit, and image-quota overage
tagging for photo_generation_log rows.

All limits enforced here are soft (nudge/log/tag), mirroring the only
existing plan-quota precedent in the codebase, usage_service's daily
message limit — nothing here blocks a conversation or an image generation.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.client_monthly_usage import ClientMonthlyUsage
from app.models.conversation import Conversation
from app.models.photo_generation_log import PhotoGenerationLog
from app.services import outbound, plan_cache

logger = logging.getLogger(__name__)


def _current_period(today: Optional[date] = None) -> str:
    """Return the given (or current) date's calendar month as 'YYYY-MM'."""
    today = today or date.today()
    return today.strftime("%Y-%m")


def _add_one_month(d: date) -> date:
    """Return the same day-of-month one calendar month after d, clamped to that month's length."""
    if d.month == 12:
        year, month = d.year + 1, 1
    else:
        year, month = d.year, d.month + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


async def ensure_current_cycle(db: AsyncSession, client: Client) -> None:
    """
    Lazily roll the client's billing cycle forward if it has elapsed.

    No cron job runs anywhere in this codebase — other per-period counters
    (UsageLog, Conversation.llm_calls_date) are also rolled forward lazily
    on next use rather than reset by a scheduler. If plan_grandfathered is
    set, snapshot terms are never refreshed automatically — a deliberate
    opt-out from live plan changes for that client.

    Args:
        db:     Active async DB session.
        client: The Client ORM instance to check/roll forward.
    """
    if client.billing_cycle_start is None:
        client.billing_cycle_start = date.today()
        await db.commit()
        return

    if client.plan_grandfathered:
        return

    next_cycle_start = _add_one_month(client.billing_cycle_start)
    if date.today() < next_cycle_start:
        return

    plan = await plan_cache.get_plan(db, client.plan_slug) or await plan_cache.get_plan(
        db, "starter"
    )
    client.plan_conv_limit_snapshot = plan["conv_limit"]
    client.plan_price_snapshot = plan["price_inr"]
    client.plan_image_quota_snapshot = plan["image_quota"]
    client.plan_image_overage_price_snapshot = plan["image_overage_price"]
    client.billing_cycle_start = next_cycle_start
    client.conv_limit_warned_period = None
    await db.commit()
    logger.info(
        "Client %d billing cycle rolled to %s (plan '%s').",
        client.id, next_cycle_start, client.plan_slug,
    )


async def record_conversation_activity(
    db: AsyncSession, client: Client, conversation: Conversation
) -> None:
    """
    Count one unique conversation toward the client's monthly conv_limit and
    send a one-time soft nudge at 80% usage. Never blocks — soft limit,
    matching usage_service.record_message's daily-message-quota pattern.

    No-ops if this conversation was already counted in the current period.

    Args:
        db:           Active async DB session.
        client:       The Client ORM instance owning this conversation.
        conversation: The Conversation being counted.
    """
    await ensure_current_cycle(db, client)
    period = _current_period()

    result = await db.execute(
        select(ClientMonthlyUsage)
        .where(
            ClientMonthlyUsage.client_id == client.id,
            ClientMonthlyUsage.period == period,
        )
        .with_for_update()
    )
    usage = result.scalar_one_or_none()

    if conversation.usage_counted_period != period:
        if usage is None:
            usage = ClientMonthlyUsage(client_id=client.id, period=period, conv_count=1)
            db.add(usage)
        else:
            usage.conv_count += 1
        conversation.usage_counted_period = period
        await db.commit()
        await db.refresh(usage)

    if usage is None:
        return

    limit = client.plan_conv_limit_snapshot or 1
    threshold_80 = int(limit * 0.8)

    if usage.conv_count >= threshold_80 and client.conv_limit_warned_period != period:
        pct = round(usage.conv_count / limit * 100)
        warning = (
            f"Usage Alert: Your AI agent has handled {usage.conv_count}/{limit} conversations "
            f"this billing cycle ({pct}%). Consider upgrading your plan to avoid interruptions."
        )
        client.conv_limit_warned_period = period
        await db.commit()
        logger.warning(
            "Client %d reached 80%% of monthly conv_limit (%d/%d).",
            client.id, usage.conv_count, limit,
        )
        if client.whatsapp_number:
            try:
                await outbound.send_owner_text(client.whatsapp_number, warning)
            except Exception as exc:
                logger.warning(
                    "Could not send conv_limit usage warning to %s: %s",
                    client.whatsapp_number, exc,
                )

    if usage.conv_count >= limit:
        logger.warning(
            "Client %d hit monthly conv_limit (%d/%d). Soft limit — agent keeps running.",
            client.id, usage.conv_count, limit,
        )


async def check_image_quota_and_bill_overage(
    db: AsyncSession, client_id: int
) -> tuple[bool, Optional[int]]:
    """
    Determine whether the next photo_generation_log row for this client is
    an overage event for the current calendar month, and at what rate.

    Call this before inserting a new photo_generation_log row so the count
    reflects prior events only (i.e. the row about to be logged is the one
    that may push the client over quota).

    Args:
        db:        Active async DB session.
        client_id: Owning client's primary key.

    Returns:
        (is_overage, overage_price_inr) — overage_price_inr is None when
        is_overage is False.
    """
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if client is None:
        return False, None

    await ensure_current_cycle(db, client)

    today = date.today()
    month_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    count_result = await db.execute(
        select(func.count()).select_from(PhotoGenerationLog).where(
            PhotoGenerationLog.client_id == client_id,
            PhotoGenerationLog.created_at >= month_start,
        )
    )
    used_this_month = count_result.scalar_one() or 0

    quota = client.plan_image_quota_snapshot
    if used_this_month >= quota:
        return True, client.plan_image_overage_price_snapshot
    return False, None
