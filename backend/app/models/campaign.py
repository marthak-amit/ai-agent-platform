"""ORM model for a WhatsApp broadcast campaign."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Campaign(Base):
    """
    One row per broadcast campaign created by a client.

    status transitions:
        draft → scheduled → running → completed
                                    → failed
    Counts are incremented in-place by campaign_service during sending.
    """

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    message_template: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Supports {name} placeholder for personalisation."
    )
    status: Mapped[str] = mapped_column(String, default="draft", nullable=False)

    # Timestamps
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Delivery counters
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    recipients: Mapped[list["CampaignRecipient"]] = relationship(
        "CampaignRecipient", back_populates="campaign", lazy="select"
    )
