"""
Conversations API router (protected — requires JWT).

Provides the React dashboard with conversation data, full message threads,
and human-takeover control.

Endpoints:
- GET  /conversations                  — paginated list with summaries
- GET  /conversations/{id}             — full conversation with all messages
- PATCH /conversations/{id}/takeover   — pause AI, enable human control
- PATCH /conversations/{id}/resume     — re-enable AI for this conversation
- POST  /conversations/{id}/send-message — save a human-typed message
"""

import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.message import Message
from app.routers.auth import get_current_client

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(get_current_client)],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConversationSummary(BaseModel):
    """Summary row for the conversation list panel."""

    id: int
    phone_number: str
    channel: str
    lead_status: str
    last_message: str
    message_count: int
    ai_enabled: bool
    updated_at: Optional[str] = None


class MessageOut(BaseModel):
    """A single message in the thread."""

    id: int
    role: str           # "user" | "assistant" | "system"
    content: str
    created_at: str


class ConversationDetail(BaseModel):
    """Full conversation with all messages."""

    id: int
    phone_number: str
    channel: str
    ai_enabled: bool
    taken_over_at: Optional[str]
    taken_over_note: Optional[str]
    lead_status: str
    message_count: int
    created_at: str
    updated_at: Optional[str]
    messages: list[MessageOut]


class TakeoverRequest(BaseModel):
    """Body for PATCH /conversations/{id}/takeover."""

    note: Optional[str] = None


class SendMessageRequest(BaseModel):
    """Body for POST /conversations/{id}/send-message."""

    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to ISO string, or None."""
    return dt.isoformat() if dt else None


async def _get_conv_or_404(db: AsyncSession, conv_id: int) -> Conversation:
    """Fetch a Conversation by id or raise 404."""
    result = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    conv = result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return conv


async def _get_lead_status(db: AsyncSession, phone_number: str) -> str:
    """Return the lead status for phone_number, defaulting to 'cold'."""
    result = await db.execute(select(Lead).where(Lead.phone_number == phone_number))
    lead = result.scalar_one_or_none()
    return lead.status if lead else "cold"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
) -> list[ConversationSummary]:
    """
    Return a paginated list of conversations ordered by most recent activity.

    Args:
        db:     Injected async DB session.
        limit:  Max rows (capped at 200).
        offset: Pagination offset.

    Returns:
        List of ConversationSummary objects.
    """
    result = await db.execute(
        select(Conversation)
        .order_by(Conversation.updated_at.desc().nullslast(), Conversation.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    conversations = result.scalars().all()

    summaries = []
    for conv in conversations:
        msg_result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_msg = msg_result.scalar_one_or_none()

        count_result = await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == conv.id)
        )
        msg_count = count_result.scalar() or 0

        lead_status = await _get_lead_status(db, conv.phone_number)

        summaries.append(
            ConversationSummary(
                id=conv.id,
                phone_number=conv.phone_number,
                channel=conv.channel,
                lead_status=lead_status,
                last_message=last_msg.content[:120] if last_msg else "",
                message_count=msg_count,
                ai_enabled=conv.ai_enabled if conv.ai_enabled is not None else True,
                updated_at=_ts(conv.updated_at),
            )
        )

    return summaries


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: int,
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """
    Return the full conversation thread with all messages.

    Args:
        conv_id: Conversation primary key.
        db:      Injected async DB session.

    Returns:
        ConversationDetail with messages array and takeover state.

    Raises:
        HTTPException 404: If conversation not found.
    """
    conv = await _get_conv_or_404(db, conv_id)

    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at)
    )
    messages = msgs_result.scalars().all()

    lead_status = await _get_lead_status(db, conv.phone_number)

    return ConversationDetail(
        id=conv.id,
        phone_number=conv.phone_number,
        channel=conv.channel,
        ai_enabled=conv.ai_enabled if conv.ai_enabled is not None else True,
        taken_over_at=_ts(conv.taken_over_at),
        taken_over_note=conv.taken_over_note,
        lead_status=lead_status,
        message_count=len(messages),
        created_at=_ts(conv.created_at) or "",
        updated_at=_ts(conv.updated_at),
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=_ts(m.created_at) or "",
            )
            for m in messages
        ],
    )


@router.patch("/{conv_id}/takeover", response_model=ConversationDetail)
async def takeover_conversation(
    conv_id: int,
    body: TakeoverRequest,
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """
    Pause the AI agent for this conversation (human takeover).

    Sets ai_enabled = False so the webhook will silently save incoming
    messages without generating or sending an AI reply. The owner can then
    reply directly via the real WhatsApp app.

    Args:
        conv_id: Conversation primary key.
        body:    Optional reason note.
        db:      Injected async DB session.

    Returns:
        Updated ConversationDetail.
    """
    conv = await _get_conv_or_404(db, conv_id)
    conv.ai_enabled = False
    conv.taken_over_at = datetime.now(timezone.utc)
    conv.taken_over_note = body.note
    await db.commit()
    await db.refresh(conv)
    logger.info("Human takeover activated for conversation %d.", conv_id)
    return await get_conversation(conv_id, db)


@router.patch("/{conv_id}/resume", response_model=ConversationDetail)
async def resume_conversation(
    conv_id: int,
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """
    Re-enable the AI agent for this conversation.

    Clears the takeover state so the next incoming message will be processed
    by the AI as normal.

    Args:
        conv_id: Conversation primary key.
        db:      Injected async DB session.

    Returns:
        Updated ConversationDetail.
    """
    conv = await _get_conv_or_404(db, conv_id)
    conv.ai_enabled = True
    conv.taken_over_at = None
    conv.taken_over_note = None
    await db.commit()
    await db.refresh(conv)
    logger.info("AI resumed for conversation %d.", conv_id)
    return await get_conversation(conv_id, db)


@router.post("/{conv_id}/send-message", status_code=status.HTTP_201_CREATED)
async def send_human_message(
    conv_id: int,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """
    Save a message typed by the business owner in the dashboard.

    Stores the message with role='human' so it appears in the chat thread
    with a distinct visual style. Does NOT send a WhatsApp/Instagram message
    (that requires a real device or verified token).

    Args:
        conv_id: Conversation primary key.
        body:    Message text from the owner.
        db:      Injected async DB session.

    Returns:
        The created MessageOut.
    """
    conv = await _get_conv_or_404(db, conv_id)

    msg = Message(
        conversation_id=conv.id,
        role="human",
        content=body.message,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    logger.info("Human message saved for conversation %d.", conv_id)
    return MessageOut(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        created_at=_ts(msg.created_at) or "",
    )
