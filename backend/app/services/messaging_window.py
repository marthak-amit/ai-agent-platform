"""
Shared guard for Meta's 24-hour customer-service window.

Meta rejects business-initiated free-form text sent more than 24h after the
customer's last inbound message — only pre-approved template messages are
allowed after that point. This centralises the check (previously only
implemented inline in scheduler.py's abandoned-intent nudge) so every
proactive outbound path can gate free-text sends the same way instead of
re-implementing — or forgetting — the cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message

WINDOW_HOURS = 24


async def get_last_inbound_at(db: AsyncSession, conversation_id: int) -> datetime | None:
    """
    Return the timestamp of the customer's most recent inbound message.

    Args:
        db:              Active async DB session.
        conversation_id: PK of the Conversation row.

    Returns:
        The most recent role='user' Message.created_at, or None if the
        conversation has no inbound messages.
    """
    result = await db.execute(
        select(Message.created_at)
        .where(Message.conversation_id == conversation_id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def is_within_free_text_window(db: AsyncSession, conversation_id: int | None) -> bool:
    """
    Check whether a free-form business-initiated text is still allowed.

    Args:
        db:              Active async DB session.
        conversation_id: PK of the Conversation row, or None.

    Returns:
        True if the customer's last inbound message was within
        WINDOW_HOURS hours ago. False if there is no conversation_id, no
        inbound message at all, or the last one is older than the window —
        in all of these cases only a pre-approved template send is allowed.
    """
    if conversation_id is None:
        return False

    last_inbound = await get_last_inbound_at(db, conversation_id)
    if last_inbound is None:
        return False

    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)

    return (datetime.now(timezone.utc) - last_inbound) <= timedelta(hours=WINDOW_HOURS)
