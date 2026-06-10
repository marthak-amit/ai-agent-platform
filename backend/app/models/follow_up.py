"""ORM model for a follow-up message sent to a warm/cold lead."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FollowUp(Base):
    """
    One row per follow-up attempt for a lead.

    status is 'sent' when whatsapp_service succeeded, 'failed' when it raised.
    The follow-up eligibility check uses sent_at to enforce the 48-hour
    re-send cooldown — both sent and failed rows count as attempts so a
    lead with a failed delivery isn't immediately retried.
    """

    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leads.id"), nullable=False, index=True
    )
    phone_number: Mapped[str] = mapped_column(String, nullable=False, index=True)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="sent", nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
