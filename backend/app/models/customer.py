"""ORM model for a deduplicated customer profile."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    """
    One row per unique (client_id, phone) pair.

    Auto-created on first message; stats (total_orders, total_spent) are
    updated each time an order is confirmed in the webhook handler.
    """

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )

    phone: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Auto-calculated stats
    total_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_spent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_order_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Preferences
    preferred_language: Mapped[str] = mapped_column(
        String, default="english", nullable=False
    )
    preferred_payment: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Tags & flags
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tags: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # comma-separated: "wholesale,regular,vip"

    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
