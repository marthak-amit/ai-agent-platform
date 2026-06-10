"""ORM model for a single message within a conversation."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Message(Base):
    """
    One row per message turn.

    role is either 'user' (customer) or 'model' (Gemini AI reply).
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Non-null when content was transcribed/substituted from a media message.
    # Values: "audio" (voice note), "image".
    original_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # WhatsApp Message ID from Meta payload — used to deduplicate retried webhooks.
    wamid: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
    )
