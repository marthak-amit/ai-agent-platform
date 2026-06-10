"""ORM model for a SaaS client (business owner) account."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant for a business. "
    "Answer customer questions professionally, help them find products or services, "
    "and guide them toward making a purchase. Be concise and friendly."
)

BUSINESS_TYPES = {"textile", "clinic", "realestate", "ecommerce", "other"}


class Client(Base):
    """
    One row per registered business using the platform.

    gemini_system_prompt is the customisable AI personality for the client's
    WhatsApp agent. Set via the Settings page in the React dashboard.

    Onboarding fields (all nullable — filled by POST /onboarding/setup-agent):
      business_type, business_description, products, whatsapp_number, api_key.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)
    business_name: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gemini_system_prompt: Mapped[str] = mapped_column(Text, default=DEFAULT_SYSTEM_PROMPT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Onboarding fields
    business_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    business_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    products: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)

    # Plan and usage limits
    plan_slug: Mapped[str] = mapped_column(String, default="starter")
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=100)

    # GST / invoicing fields
    gst_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    business_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hsn_code: Mapped[str] = mapped_column(String, default="5007")

    # Channel credentials (stored per-client for multi-tenant support)
    whatsapp_phone_number_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    whatsapp_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instagram_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instagram_account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Daily briefing settings
    briefing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    briefing_time: Mapped[str] = mapped_column(String, default="09:00")

    # Dashboard UI language preference: "en", "hi", or "gu"
    dashboard_language: Mapped[str] = mapped_column(String, default="en")

    # Public catalogue fields
    catalogue_slug: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True, index=True)
    logo_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    banner_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    catalogue_tagline: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    catalogue_theme_color: Mapped[str] = mapped_column(String, default="#6366F1")

    # Payment settings
    # accepts_cod: when False the agent only offers UPI (default for textile traders)
    accepts_cod: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # upi_id: fallback UPI handle sent as text when Razorpay is not configured
    upi_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Onboarding wizard progress (migration 0029)
    # 0=registered, 1=profile, 2=products, 3=agent, 4=whatsapp, 5=tested, 6=complete
    onboarding_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
