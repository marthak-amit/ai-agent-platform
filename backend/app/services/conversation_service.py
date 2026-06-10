"""
Conversation and message persistence service.

Provides get-or-create semantics for conversations and append-only
message storage. Used by both the WhatsApp and Instagram webhook handlers.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.lead import Lead

_ROLE_MAP = {
    "human": "user",
    "agent": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "you": "user",
    "owner": "user",
    "model": "assistant",
    "user": "user",
    "assistant": "assistant",
    "system": "system",
}


def normalize_role(role: str) -> str:
    """Map any incoming role string to a Groq-safe value ('user', 'assistant', 'system')."""
    return _ROLE_MAP.get(role.lower().strip(), "user")


async def get_or_create_conversation(
    db: AsyncSession,
    phone_number: str,
    channel: str = "whatsapp",
    is_sandbox: bool = False,
) -> Conversation:
    """
    Return an existing conversation for this customer or create a new one.

    Args:
        db:           Active async DB session.
        phone_number: WhatsApp E.164 number or Instagram IGSID.
        channel:      'whatsapp' or 'instagram'.
        is_sandbox:   When True, scopes lookup/creation to sandbox conversations only.

    Returns:
        Conversation instance (persisted).
    """
    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone_number)
        .where(Conversation.channel == channel)
        .where(Conversation.is_sandbox == is_sandbox)
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        conv = Conversation(phone_number=phone_number, channel=channel, is_sandbox=is_sandbox)
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    return conv


async def delete_sandbox_conversation(
    db: AsyncSession,
    phone_number: str,
    channel: str = "sandbox",
) -> None:
    """
    Delete a sandbox conversation and all its messages so the client gets a fresh start.

    Args:
        db:           Active async DB session.
        phone_number: The sandbox phone identifier (e.g. 'sandbox_<client_id>').
        channel:      Channel key for sandbox conversations.
    """
    from sqlalchemy import delete as sql_delete

    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone_number)
        .where(Conversation.channel == channel)
        .where(Conversation.is_sandbox == True)  # noqa: E712
    )
    conv = result.scalar_one_or_none()
    if conv:
        await db.execute(
            sql_delete(Message).where(Message.conversation_id == conv.id)
        )
        await db.execute(
            sql_delete(Conversation).where(Conversation.id == conv.id)
        )
        await db.commit()


async def save_message(
    db: AsyncSession,
    conversation_id: int,
    role: str,
    content: str,
    original_type: str | None = None,
    wamid: str | None = None,
) -> Message:
    """
    Append a message to a conversation.

    Args:
        db:              Active async DB session.
        conversation_id: FK to the parent Conversation.
        role:            'user' or 'model'.
        content:         Raw text of the message.
        original_type:   Media type the content was derived from ('audio', 'image'), or None.
        wamid:           WhatsApp Message ID from Meta for deduplication, or None.

    Returns:
        Persisted Message instance.
    """
    msg = Message(
        conversation_id=conversation_id,
        role=normalize_role(role),
        content=content,
        original_type=original_type,
        wamid=wamid,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_history(
    db: AsyncSession,
    conversation_id: int,
    limit: int = 20,
) -> list[Message]:
    """
    Return the last `limit` messages for a conversation, oldest first.

    Args:
        db:              Active async DB session.
        conversation_id: FK to the parent Conversation.
        limit:           Maximum number of messages to return.

    Returns:
        List of Message instances ordered by created_at ascending.
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    messages = list(reversed(result.scalars().all()))
    for msg in messages:
        msg.role = normalize_role(msg.role)
    logger.debug("get_history: conversation %d — %d messages loaded.", conversation_id, len(messages))
    return messages


async def update_stage(
    db: AsyncSession,
    conversation_id: int,
    stage: str,
) -> None:
    """
    Persist the latest conversation stage to the DB.

    Args:
        db:              Active async DB session.
        conversation_id: PK of the Conversation row.
        stage:           Stage key from conversation_flow.STAGES.
    """
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if conv:
        conv.current_stage = stage
        await db.commit()


async def update_language(
    db: AsyncSession,
    conversation_id: int,
    language: str,
) -> None:
    """
    Persist the latest non-ambiguous customer language to the DB.

    Only called when the detected language is not derived from a fallback
    (i.e. the message was not an ambiguous single-word reply).

    Args:
        db:              Active async DB session.
        conversation_id: PK of the Conversation row.
        language:        Language code from language_service.detect_language().
    """
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if conv:
        conv.last_customer_language = language
        await db.commit()


async def update_order_field(
    db: AsyncSession,
    conversation_id: int,
    field: str,
    value,
) -> None:
    """
    Persist a single order-collection field (quantity, name, or address) so
    progress survives across turns and a customer who drops mid-flow can
    resume without being re-asked.

    Args:
        db:              Active async DB session.
        conversation_id: PK of the Conversation row.
        field:           One of 'pending_order_quantity', 'customer_name',
                         'delivery_address'.
        value:           The extracted value to store.
    """
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if conv:
        setattr(conv, field, value)
        await db.commit()


async def get_customer_history(
    db: AsyncSession,
    phone_number: str,
    client_id: int,
) -> dict:
    """
    Return a lightweight order-history summary for repeat-customer detection.

    Counts completed-stage conversations for this phone number and tries to
    surface the last product mentioned in those conversations.

    Args:
        db:           Active async DB session.
        phone_number: Customer's WhatsApp E.164 number or IGSID.
        client_id:    Owning client's PK (unused directly but kept for future
                      multi-tenant filtering).

    Returns:
        Dict with total_orders (int), last_product (str|None), address (str|None).
    """
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.phone_number == phone_number,
            Conversation.current_stage == "completed",
        )
        .order_by(Conversation.updated_at.desc())
    )
    past_convs = list(result.scalars().all())
    total_orders = len(past_convs)

    last_product: str | None = None
    address: str | None = None

    if past_convs:
        latest = past_convs[0]
        address = latest.delivery_address
        # Try to extract last product from messages of most recent completed conv
        msgs_result = await db.execute(
            select(Message)
            .where(Message.conversation_id == latest.id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        for msg in msgs_result.scalars().all():
            if msg.role == "assistant" and msg.content:
                # Simple heuristic: first line of AI reply in order_collection stage
                first_line = msg.content.split("\n")[0]
                if "₹" in first_line or any(
                    kw in first_line.lower()
                    for kw in ["saree", "kurti", "lehenga", "product", "order"]
                ):
                    last_product = first_line[:60]
                    break

    return {
        "total_orders": total_orders,
        "last_product": last_product,
        "address": address,
    }
