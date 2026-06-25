"""ORM model for a sales lead derived from a conversation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Lead(Base):
    """
    One row per unique customer (client_id, channel, phone_number / IGSID).

    status is one of 'hot', 'warm', or 'cold' and is re-evaluated after
    each conversation turn by lead_service.tag_lead.
    """

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "channel", "phone_number",
            name="uq_leads_client_channel_phone",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="cold")

    # Tenant owner — NOT NULL + part of the composite unique key above
    # (client_id, channel, phone_number) since the contract stage of the
    # identity migration (migration 0047).
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    # channel mirrors Conversation.channel; defaults to 'whatsapp' for all
    # pre-existing rows since the platform was WhatsApp-only until Instagram launched.
    channel: Mapped[str] = mapped_column(String, nullable=False, server_default="whatsapp")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
