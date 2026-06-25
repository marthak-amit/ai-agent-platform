"""ORM model for inbound demo requests submitted from the public marketing site."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DemoLead(Base):
    """
    A prospective merchant's "Book a Demo" request from the marketing site.

    Distinct from Lead (app/models/lead.py), which tracks a merchant's own
    end-customers derived from chat conversations. DemoLead has no client_id —
    it is not scoped to any tenant; it is inbound interest in becoming one.
    """

    __tablename__ = "demo_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    monthly_order_volume: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String, default="marketing_site")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
