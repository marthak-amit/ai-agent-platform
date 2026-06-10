"""
Follow-up engine service.

Finds warm and cold WhatsApp leads whose last conversation message is older
than 24 hours and who have not received a follow-up in the last 48 hours,
then sends a personalised re-engagement message via WhatsApp.

Public API:
    get_eligible_leads(db)         → list[(Lead, last_msg_at)]
    generate_followup_message(db, lead) → str
    send_followups(db)             → dict  {sent, failed, total_eligible}
    get_stats(db)                  → dict  {sent_today, sent_last_7_days, currently_eligible}
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.follow_up import FollowUp
from app.models.lead import Lead
from app.models.message import Message
from app.services import conversation_service, gemini_service, instagram_service, whatsapp_service

logger = logging.getLogger(__name__)

# Re-engagement template used when Gemini is unavailable or has no context.
_DEFAULT_MESSAGE = (
    "Namaste! 🙏 Aapne hamare products mein interest dikhaya tha. "
    "Aapka order complete karna baaki hai — kya aapko koi help chahiye? "
    "Hum yahan hain! 😊"
)

# Eligibility windows
_LAST_MSG_HOURS = 24   # lead must have been silent for at least this long
_COOLDOWN_HOURS = 48   # minimum gap between two follow-ups to the same lead


async def get_eligible_leads(
    db: AsyncSession,
) -> list[tuple[Lead, datetime]]:
    """
    Return leads that meet all three follow-up criteria:

    1. Lead status is 'warm' or 'cold' (hot leads are actively engaged).
    2. The conversation is on WhatsApp (only channel that supports outbound).
    3. The last message in the conversation is older than _LAST_MSG_HOURS.
    4. No follow-up row exists for this lead within the last _COOLDOWN_HOURS.

    Args:
        db: Active async DB session.

    Returns:
        List of (Lead, last_message_at) tuples ordered oldest-first so the
        leads who have been waiting longest are contacted first.
    """
    now = datetime.now(timezone.utc)
    cutoff_msg = now - timedelta(hours=_LAST_MSG_HOURS)
    cutoff_fu  = now - timedelta(hours=_COOLDOWN_HOURS)

    # Subquery: most recent message timestamp per conversation (both channels)
    last_msg_subq = (
        select(
            Message.conversation_id,
            func.max(Message.created_at).label("last_msg_at"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )

    # Correlated subquery: was a follow-up sent or attempted within the cooldown?
    recent_followup_exists = (
        select(FollowUp.id)
        .where(
            FollowUp.lead_id == Lead.id,
            FollowUp.sent_at >= cutoff_fu,
        )
        .exists()
    )

    stmt = (
        select(Lead, last_msg_subq.c.last_msg_at)
        .join(Conversation, Lead.conversation_id == Conversation.id)
        .join(last_msg_subq, Conversation.id == last_msg_subq.c.conversation_id)
        .where(
            Lead.status.in_(["warm", "cold"]),
            Conversation.channel.in_(["whatsapp", "instagram"]),
            Lead.conversation_id.is_not(None),
            last_msg_subq.c.last_msg_at < cutoff_msg,
            ~recent_followup_exists,
        )
        .order_by(last_msg_subq.c.last_msg_at.asc())
    )

    result = await db.execute(stmt)
    rows = result.all()
    return [(row.Lead, row.last_msg_at) for row in rows]


async def generate_followup_message(db: AsyncSession, lead: Lead) -> str:
    """
    Generate a personalised re-engagement message for a lead using Gemini.

    Loads the lead's conversation history and asks Gemini to write a brief,
    warm follow-up in Hindi/Hinglish that references their specific interest.
    Falls back to _DEFAULT_MESSAGE if the conversation is empty or Gemini fails.

    Args:
        db:   Active async DB session.
        lead: The Lead to generate a message for.

    Returns:
        Plain text WhatsApp message to send to the customer.
    """
    if lead.conversation_id is None:
        return _DEFAULT_MESSAGE

    history = await conversation_service.get_history(db, lead.conversation_id)
    if not history:
        return _DEFAULT_MESSAGE

    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    prompt = (
        "You are a helpful AI for an Indian business on WhatsApp. "
        "Based on this conversation, write a single brief, friendly re-engagement message "
        "in Hindi/Hinglish to a customer who showed interest but has not completed their purchase. "
        "Keep it under 2 sentences. Be warm and helpful — not pushy or spammy. "
        "Reference their specific interest if it is clear from the conversation. "
        "End with an open question like 'Kya main aapki help kar sakta hoon?'"
    )

    try:
        return await gemini_service.generate_reply(prompt, history=history_dicts)
    except Exception as exc:
        logger.warning(
            "Gemini follow-up generation failed for lead %d: %s. Using default message.",
            lead.id, exc,
        )
        return _DEFAULT_MESSAGE


async def send_followups(db: AsyncSession) -> dict:
    """
    Run one complete follow-up cycle: find eligible leads, generate messages,
    send via WhatsApp, and persist every attempt (success or failure).

    A FollowUp row is written for both 'sent' and 'failed' outcomes so the
    cooldown check prevents immediate retries on a failed delivery.

    Args:
        db: Active async DB session.

    Returns:
        Dict with keys: sent (int), failed (int), total_eligible (int).
    """
    eligible = await get_eligible_leads(db)
    sent = 0
    failed = 0

    for lead, last_msg_at in eligible:
        message = await generate_followup_message(db, lead)
        fu_status = "sent"

        # Determine channel from the lead's conversation
        channel = "whatsapp"
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == lead.conversation_id).limit(1)
        )
        conv = conv_result.scalar_one_or_none()
        if conv:
            channel = conv.channel or "whatsapp"

        try:
            if channel == "instagram":
                from app.models.client import Client
                # Resolve client to get instagram_account_id
                client_result = await db.execute(
                    select(Client).where(Client.id == conv.client_id).limit(1)
                ) if conv and getattr(conv, "client_id", None) else None
                client = client_result.scalar_one_or_none() if client_result else None
                ig_user_id = getattr(client, "instagram_account_id", None) if client else None
                if ig_user_id:
                    await instagram_service.send_dm(
                        ig_user_id=ig_user_id,
                        recipient_igsid=lead.phone_number,
                        message_text=message,
                    )
                else:
                    raise ValueError("No instagram_account_id for client — cannot send Instagram follow-up.")
            else:
                await whatsapp_service.send_text_message(
                    to_phone_number=lead.phone_number,
                    message_text=message,
                )
            sent += 1
        except Exception as exc:
            logger.error(
                "Follow-up send failed for lead %d (%s, channel=%s): %s",
                lead.id, lead.phone_number, channel, exc,
            )
            fu_status = "failed"
            failed += 1

        fu = FollowUp(
            lead_id=lead.id,
            phone_number=lead.phone_number,
            message_text=message,
            status=fu_status,
        )
        db.add(fu)
        await db.commit()

        silence_hours = (datetime.now(timezone.utc) - last_msg_at).total_seconds() / 3600
        logger.info(
            "Follow-up %s — lead %d (%s) — silent %.1fh.",
            fu_status, lead.id, lead.phone_number, silence_hours,
        )

    return {
        "sent": sent,
        "failed": failed,
        "total_eligible": len(eligible),
    }


async def get_stats(db: AsyncSession) -> dict:
    """
    Return follow-up delivery statistics for the dashboard.

    Args:
        db: Active async DB session.

    Returns:
        Dict with sent_today, sent_last_7_days, failed_last_7_days,
        and currently_eligible (leads that would receive a follow-up right now).
    """
    now = datetime.now(timezone.utc)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = now - timedelta(days=7)

    today_result = await db.execute(
        select(func.count(FollowUp.id)).where(
            FollowUp.sent_at >= start_of_today,
            FollowUp.status == "sent",
        )
    )
    sent_today: int = today_result.scalar() or 0

    week_sent_result = await db.execute(
        select(func.count(FollowUp.id)).where(
            FollowUp.sent_at >= seven_days_ago,
            FollowUp.status == "sent",
        )
    )
    sent_last_7_days: int = week_sent_result.scalar() or 0

    week_failed_result = await db.execute(
        select(func.count(FollowUp.id)).where(
            FollowUp.sent_at >= seven_days_ago,
            FollowUp.status == "failed",
        )
    )
    failed_last_7_days: int = week_failed_result.scalar() or 0

    eligible = await get_eligible_leads(db)

    return {
        "sent_today": sent_today,
        "sent_last_7_days": sent_last_7_days,
        "failed_last_7_days": failed_last_7_days,
        "currently_eligible": len(eligible),
    }
