"""
Lead classification service.

Classifies customer buying intent as 'hot', 'warm', or 'cold' using
intent_service.classify_lead() — a stage-first, keyword-fallback classifier
that is more accurate than the old pure-keyword _classify() approach.

Specifically fixes false positives like "delivery kitne din mein?" being
classified as "hot" when the customer is just browsing.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.services.intent_service import classify_lead

logger = logging.getLogger(__name__)


async def tag_lead(
    db: AsyncSession,
    phone_number: str,
    conversation_id: int,
    messages: list[dict],
    client_id: int | None = None,
    channel: str = "whatsapp",
) -> Lead:
    """
    Classify a conversation and upsert the lead record.

    Uses intent_service.classify_lead() which checks the conversation stage
    first (more accurate) and falls back to keyword scoring.

    Args:
        db:              Active async DB session.
        phone_number:    Customer identifier (E.164 or IGSID).
        conversation_id: FK to the parent Conversation.
        messages:        List of dicts with 'role' and 'content' keys.
        client_id:       Owning client's PK, when already resolved by the
                         caller. Scopes the lookup and is stamped onto newly
                         created rows.
        channel:         Channel this lead came in on ('whatsapp', 'instagram',
                         'website'). Stamped onto newly created rows.

    Returns:
        Updated or created Lead instance.
    """
    # Resolve conversation stage for stage-first classification
    stage = "greeting"
    try:
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id).limit(1)
        )
        conv = conv_result.scalar_one_or_none()
        if conv:
            stage = conv.current_stage or "greeting"
    except Exception as exc:
        logger.warning("Could not load conversation stage for lead tagging: %s", exc)

    # Extract latest customer message for keyword scoring fallback
    customer_msgs = [m for m in messages if m.get("role") == "user"]
    latest_message = customer_msgs[-1]["content"] if customer_msgs else ""

    status = classify_lead(latest_message, stage)
    logger.debug(
        "tag_lead: phone=%s stage=%s latest=%r → %s",
        phone_number, stage, latest_message[:60], status,
    )

    lead_query = select(Lead).where(Lead.phone_number == phone_number)
    if client_id is not None:
        lead_query = lead_query.where(Lead.client_id == client_id)
    result = await db.execute(lead_query)
    lead = result.scalar_one_or_none()
    if lead is None:
        lead = Lead(
            phone_number=phone_number,
            conversation_id=conversation_id,
            status=status,
            client_id=client_id,
            channel=channel,
        )
        db.add(lead)
    else:
        lead.status = status
        lead.conversation_id = conversation_id

    await db.commit()
    await db.refresh(lead)
    return lead
