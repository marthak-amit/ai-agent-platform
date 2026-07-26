"""Pydantic request/response schemas for plan management (client + admin)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class PlanOut(BaseModel):
    """Public representation of a plan (GET /plans, /plans/current)."""

    slug: str
    name: str
    price_inr: int
    conv_limit: int
    image_quota: int
    image_overage_price: int
    daily_msg_limit: int
    channels: list[str]
    description: str


class UpgradeRequest(BaseModel):
    """Request body for POST /plans/upgrade."""

    plan_slug: str


class UpgradeResponse(BaseModel):
    """Response for a successful plan upgrade."""

    previous_plan: str
    new_plan: PlanOut
    message: str


class PlanAdminOut(BaseModel):
    """Full plan row as seen by the admin panel (GET/PUT /admin/plans)."""

    plan_id: str
    name: str
    price_inr: int
    conv_limit: int
    image_quota: int
    image_overage_price: int
    daily_msg_limit: int
    channels: list[str]
    campaign_allowed: bool
    campaign_max_recipients: int
    campaign_monthly_limit: int
    tier_order: int
    description: str
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PlanUpdateRequest(BaseModel):
    """
    Request body for PUT /admin/plans/{plan_id}.

    Every field is optional — only the ones provided are updated. Changes
    take effect immediately for new signups and existing clients' next
    billing cycle; they never retroactively alter a client mid-cycle.
    """

    name: Optional[str] = None
    price_inr: Optional[int] = None
    conv_limit: Optional[int] = None
    image_quota: Optional[int] = None
    image_overage_price: Optional[int] = None
    daily_msg_limit: Optional[int] = None
    channels: Optional[list[str]] = None
    campaign_allowed: Optional[bool] = None
    campaign_max_recipients: Optional[int] = None
    campaign_monthly_limit: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator(
        "price_inr", "conv_limit", "image_quota", "image_overage_price",
        "daily_msg_limit", "campaign_max_recipients", "campaign_monthly_limit",
    )
    @classmethod
    def _non_negative(cls, value: Optional[int]) -> Optional[int]:
        """Reject negative numeric fields when provided."""
        if value is not None and value < 0:
            raise ValueError("must be zero or greater")
        return value
