"""
Daily morning briefing service.

Generates a WhatsApp-formatted summary of yesterday's activity for each
active client and sends it to their registered owner phone number.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import whatsapp_service

logger = logging.getLogger(__name__)


# ── Per-stat helpers ──────────────────────────────────────────────────────────

async def _get_message_count(client_id: int, day: date, db: AsyncSession) -> int:
    """Return total messages logged for *client_id* on *day* from UsageLog."""
    from app.models.usage_log import UsageLog

    result = await db.execute(
        select(UsageLog.message_count).where(
            UsageLog.client_id == client_id,
            UsageLog.date == day,
        )
    )
    row = result.scalar_one_or_none()
    return row or 0


async def _get_lead_count(client_id: int, status: str, db: AsyncSession) -> int:
    """Return total leads with given *status* for *client_id*."""
    from app.models.lead import Lead
    from app.models.conversation import Conversation

    result = await db.execute(
        select(func.count()).select_from(Lead).join(
            Conversation, Lead.conversation_id == Conversation.id
        ).where(
            Lead.status == status,
            Conversation.client_id == client_id,
        )
    )
    return result.scalar_one() or 0


async def _get_new_conversations(client_id: int, day: date, db: AsyncSession) -> int:
    """Return number of conversations created on *day*."""
    from app.models.conversation import Conversation
    from datetime import datetime, timezone

    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    result = await db.execute(
        select(func.count()).select_from(Conversation).where(
            Conversation.client_id == client_id,
            Conversation.created_at >= day_start,
            Conversation.created_at < day_end,
        )
    )
    return result.scalar_one() or 0


async def _get_orders_and_revenue(
    client_id: int, day: date, db: AsyncSession
) -> tuple[int, float]:
    """Return (orders_placed, revenue_inr) for paid orders on *day* for *client_id*."""
    from app.models.order import Order
    from datetime import datetime, timezone

    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    result = await db.execute(
        select(func.count(), func.sum(Order.total_amount)).where(
            Order.client_id == client_id,
            Order.payment_status == "paid",
            Order.created_at >= day_start,
            Order.created_at < day_end,
        )
    )
    row = result.one()
    orders = row[0] or 0
    revenue = row[1] or 0.0
    return orders, float(revenue)


# ── Core briefing logic ───────────────────────────────────────────────────────

async def generate_daily_briefing(client_id: int, db: AsyncSession) -> str:
    """
    Build the WhatsApp briefing message string for a single client.

    Args:
        client_id: Client row ID.
        db:        Active async DB session.

    Returns:
        Formatted WhatsApp text (markdown-safe: *bold*, _italic_).
    """
    yesterday = date.today() - timedelta(days=1)

    total_messages = await _get_message_count(client_id, yesterday, db)
    hot_leads = await _get_lead_count(client_id, "hot", db)
    warm_leads = await _get_lead_count(client_id, "warm", db)
    new_conversations = await _get_new_conversations(client_id, yesterday, db)
    orders_placed, revenue = await _get_orders_and_revenue(client_id, yesterday, db)

    briefing = (
        f"🌅 *Good morning!*\n\n"
        f"📊 *Yesterday's Summary — {yesterday.strftime('%d %b %Y')}*\n\n"
        f"💬 Total messages: {total_messages}\n"
        f"🔥 Hot leads: {hot_leads}\n"
        f"🌡️ Warm leads: {warm_leads}\n"
        f"🆕 New conversations: {new_conversations}\n"
        f"📦 Orders placed: {orders_placed}\n"
        f"💰 Revenue: ₹{revenue:,.0f}\n"
    )

    if hot_leads > 0:
        briefing += f"\n⚡ *Action needed:* {hot_leads} hot lead{'s' if hot_leads > 1 else ''} waiting for follow-up!"
    if orders_placed > 0:
        briefing += f"\n✅ Great job! {orders_placed} order{'s' if orders_placed > 1 else ''} placed yesterday."

    briefing += "\n\n_Powered by your AI Agent Platform_"
    return briefing


async def send_daily_briefings(db: AsyncSession) -> None:
    """
    Send morning briefings to all active clients who have opted in and have
    a registered owner phone number.

    Args:
        db: Active async DB session.
    """
    from app.models.client import Client

    result = await db.execute(
        select(Client).where(
            Client.is_active == True,  # noqa: E712
            Client.briefing_enabled == True,  # noqa: E712
        )
    )
    clients = result.scalars().all()

    for client in clients:
        if not client.phone:
            logger.info(
                "Skipping briefing for client %d — no owner phone set.", client.id
            )
            continue
        try:
            briefing = await generate_daily_briefing(client.id, db)
            await whatsapp_service.send_text_message(
                to_phone_number=client.phone,
                message_text=briefing,
            )
            logger.info("Briefing sent to client %d (%s).", client.id, client.phone)
        except Exception as exc:
            logger.error(
                "Failed to send briefing to client %d: %s", client.id, exc
            )
