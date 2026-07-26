"""ORM model for a subscription plan (pricing/limits configuration)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Plan(Base):
    """
    One row per subscription tier (e.g. starter, growth, pro).

    plan_id is the same slug historically stored in Client.plan_slug, so no
    data migration is needed for existing clients. All pricing/limit values
    are mutable at runtime via the admin API — never hardcode them elsewhere.
    """

    __tablename__ = "plans"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    price_inr: Mapped[int] = mapped_column(Integer, nullable=False)
    conv_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    image_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    image_overage_price: Mapped[int] = mapped_column(Integer, nullable=False)

    # Folded in from the old plan_service.PLANS dict (channel gating).
    daily_msg_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    channels: Mapped[Any] = mapped_column(JSONB, nullable=False)

    # Folded in from the old campaign_service._PLAN_LIMITS dict.
    campaign_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    campaign_max_recipients: Mapped[int] = mapped_column(Integer, nullable=False)
    campaign_monthly_limit: Mapped[int] = mapped_column(Integer, nullable=False)

    # Replaces the old _PLAN_ORDER list — lower tier_order upgrades to higher.
    tier_order: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )
