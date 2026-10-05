"""ORM model for a Meta-approved message template usable outside the 24h window."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MessageTemplate(Base):
    """
    A WhatsApp message template registered for a client.

    category 'utility' templates are the ones offered to staff when the 24h
    free-form window is closed (the dashboard-send endpoint lists those in its
    WINDOW_CLOSED error). Only status='approved' rows are ever offered.
    """

    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("client_id", "name", "language", name="uq_message_templates_client_name_lang"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False, default="en", server_default="en")
    category: Mapped[str] = mapped_column(String, nullable=False, default="utility", server_default="utility")
    status: Mapped[str] = mapped_column(String, nullable=False, default="approved", server_default="approved")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
