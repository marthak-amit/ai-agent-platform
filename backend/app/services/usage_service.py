"""
Usage tracking service.

Records per-client daily message counts in UsageLog and enforces a
soft daily limit with:
  - 80% threshold: one-time WhatsApp warning to the business owner
  - 100% limit:    log-only (agent keeps replying — soft limit)
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.usage_log import UsageLog
from app.services import whatsapp_service

logger = logging.getLogger(__name__)


async def record_message(db: AsyncSession, client: Client) -> UsageLog:
    """
    Increment today's message count for a client and check usage thresholds.

    The 80% warning is sent only once — when the count crosses the threshold
    boundary (old_count < threshold <= new_count). The 100% limit is logged
    but does NOT block further messages.

    Args:
        db:     Active async DB session.
        client: The authenticated or active Client ORM instance.

    Returns:
        The updated UsageLog for today.
    """
    today = date.today()
    limit = client.daily_message_limit or 100
    threshold_80 = int(limit * 0.8)

    # SELECT FOR UPDATE locks the row for the duration of this transaction so
    # two concurrent webhook hits for the same client never both read the same
    # count, increment in Python, and write back the same value (lost update).
    result = await db.execute(
        select(UsageLog)
        .where(
            UsageLog.client_id == client.id,
            UsageLog.date == today,
        )
        .with_for_update()
    )
    log = result.scalar_one_or_none()
    old_count = log.message_count if log else 0

    if log is None:
        log = UsageLog(client_id=client.id, date=today, message_count=1)
        db.add(log)
    else:
        log.message_count += 1

    await db.commit()
    await db.refresh(log)
    new_count = log.message_count

    # 80% boundary — fire warning exactly once
    if old_count < threshold_80 <= new_count:
        pct = round(new_count / limit * 100)
        warning = (
            f"Usage Alert: Your AI agent has processed {new_count}/{limit} messages today "
            f"({pct}% of your daily limit). The agent will keep running; please monitor usage."
        )
        logger.warning(
            "Client %d reached 80%% of daily limit (%d/%d messages).",
            client.id, new_count, limit,
        )
        if client.whatsapp_number:
            try:
                await whatsapp_service.send_text_message(
                    to_phone_number=client.whatsapp_number,
                    message_text=warning,
                )
            except Exception as exc:
                logger.warning("Could not send 80%% usage warning to %s: %s", client.whatsapp_number, exc)

    # 100% soft limit — log only
    if new_count >= limit:
        logger.warning(
            "Client %d hit daily limit (%d/%d). Agent still running (soft limit).",
            client.id, new_count, limit,
        )

    return log


async def get_stats(db: AsyncSession, client: Client) -> dict:
    """
    Return usage statistics for today and the current calendar month.

    Args:
        db:     Active async DB session.
        client: The authenticated Client ORM instance.

    Returns:
        Dict with today_count, monthly_count, limit, and percentage_used (0–100).
    """
    today = date.today()
    first_of_month = today.replace(day=1)
    limit = client.daily_message_limit or 100

    today_result = await db.execute(
        select(UsageLog).where(
            UsageLog.client_id == client.id,
            UsageLog.date == today,
        )
    )
    today_log = today_result.scalar_one_or_none()
    today_count = today_log.message_count if today_log else 0

    monthly_result = await db.execute(
        select(func.sum(UsageLog.message_count)).where(
            UsageLog.client_id == client.id,
            UsageLog.date >= first_of_month,
        )
    )
    monthly_count = int(monthly_result.scalar() or 0)

    percentage_used = round(today_count / limit * 100, 1) if limit > 0 else 0.0

    return {
        "today_count": today_count,
        "monthly_count": monthly_count,
        "limit": limit,
        "percentage_used": percentage_used,
    }
