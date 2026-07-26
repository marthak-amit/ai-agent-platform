"""ORM model for per-client, per-calendar-month conversation usage tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClientMonthlyUsage(Base):
    """
    One row per client per billing month ("YYYY-MM").

    conv_count is incremented by billing_service the first time each
    conversation is active in a given month (deduped via
    Conversation.usage_counted_period), and checked against the client's
    plan_conv_limit_snapshot for the soft upgrade nudge.
    """

    __tablename__ = "client_monthly_usage"
    __table_args__ = (
        UniqueConstraint("client_id", "period", name="uq_client_monthly_usage_client_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String, nullable=False)
    conv_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )
