"""ORM model for daily per-client message usage tracking."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UsageLog(Base):
    """
    One row per client per calendar day.

    message_count is incremented by usage_service each time the AI agent
    successfully handles a customer message. The unique constraint on
    (client_id, date) ensures a single authoritative row per day.
    """

    __tablename__ = "usage_logs"
    __table_args__ = (UniqueConstraint("client_id", "date", name="uq_usage_client_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )
