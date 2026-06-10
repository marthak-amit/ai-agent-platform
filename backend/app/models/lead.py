"""ORM model for a sales lead derived from a conversation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Lead(Base):
    """
    One row per unique customer phone number / IGSID.

    status is one of 'hot', 'warm', or 'cold' and is re-evaluated after
    each conversation turn by lead_service.tag_lead.
    """

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, unique=True, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="cold")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
