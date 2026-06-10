"""
Learning service.

Mines past successful conversations to produce few-shot examples that are
injected into the system prompt, helping the agent mirror proven reply patterns.

A "successful" conversation is one that reached the 'completed' stage
(i.e. an order was placed).  The most recent matching conversations are
chosen and the first 6 messages (3 turns) are excerpted as examples.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_live_examples_for_prompt(
    client_id: int,
    current_message: str,
    db: AsyncSession,
    max_examples: int = 2,
    lookback_days: int = 30,
) -> str:
    """
    Return a few-shot example block from recent successful conversations.

    Searches completed conversations from the last *lookback_days* days whose
    messages overlap with *current_message* keywords.  Falls back to the most
    recently completed conversations when no keyword match is found.

    Args:
        client_id:       Owning client ID.
        current_message: Customer's current message text (used for keyword matching).
        db:              Async DB session.
        max_examples:    Maximum number of example conversations to include.
        lookback_days:   How many days back to search.

    Returns:
        Formatted string block ready for prompt injection, or "" when no
        suitable examples exist.
    """
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.order import Order

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Find conversation IDs that have a completed order for this client
    order_result = await db.execute(
        select(Order.conversation_id).where(
            Order.client_id == client_id,
            Order.created_at > cutoff,
        ).limit(50)
    )
    completed_conv_ids = [row[0] for row in order_result if row[0] is not None]

    if not completed_conv_ids:
        logger.info("Learning service: no completed conversations found for client %s", client_id)
        return ""

    # Load those conversations
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id.in_(completed_conv_ids),
        ).order_by(Conversation.updated_at.desc()).limit(20)
    )
    conversations = conv_result.scalars().all()

    if not conversations:
        return ""

    # Score conversations by keyword overlap with current message
    query_words = set(current_message.lower().split())

    async def _get_messages(conv_id: int) -> list[Message]:
        msg_result = await db.execute(
            select(Message).where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc())
            .limit(10)
        )
        return list(msg_result.scalars().all())

    scored: list[tuple[int, Conversation]] = []
    for conv in conversations:
        msgs = await _get_messages(conv.id)
        conv_text = " ".join(m.content for m in msgs if m.role == "user").lower()
        overlap = len(query_words & set(conv_text.split()))
        scored.append((overlap, conv))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_convs = [c for _, c in scored[:max_examples]]

    logger.info(
        "Learning service: found %d similar conversation(s) for client %s query=%r",
        len(top_convs),
        client_id,
        current_message[:60],
    )

    if not top_convs:
        return ""

    blocks: list[str] = []
    for conv in top_convs:
        msgs = await _get_messages(conv.id)
        # Take up to first 6 messages (3 turns)
        excerpt = msgs[:6]
        if len(excerpt) < 2:
            continue
        lines = []
        for m in excerpt:
            role_label = "Customer" if m.role == "user" else "Agent"
            lines.append(f"{role_label}: {m.content.strip()}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    examples_text = "\n\n---\n".join(blocks)
    return (
        "EXAMPLES FROM SUCCESSFUL PAST ORDERS (mirror this style):\n"
        f"{examples_text}\n"
    )
