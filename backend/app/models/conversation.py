"""ORM model for a customer conversation thread."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.message import Message


class Conversation(Base):
    """
    One row per unique customer (phone_number + channel).

    channel is either 'whatsapp' or 'instagram'.
    phone_number stores the WhatsApp E.164 number or Instagram IGSID.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String, default="whatsapp")

    # Human-takeover fields (migration 0010)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    taken_over_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    taken_over_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    # Conversation flow / sales funnel fields (migration 0021)
    current_stage: Mapped[str] = mapped_column(String, default="greeting", nullable=False, server_default="greeting")
    order_intent_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    customer_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pending_order_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Payment method chosen by customer during order collection (migration 0024)
    payment_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Pinned product SKU — set when customer quotes a SKU, ensures correct product
    # is used for all subsequent stock checks and order creation (migration 0030)
    pending_product_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Tracks whether the order summary has been shown to prevent re-showing it
    # and to correctly advance to the payment step (migration 0031)
    summary_shown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # Variant selections — only populated when product.has_variants=True (migration 0032/0033)
    selected_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    selected_size: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    selected_material: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # SKU mentioned mid-order when customer may want to switch products (migration 0034)
    interrupted_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Sandbox flag — sandbox conversations are excluded from analytics/leads (migration 0028)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # Language persistence — stores the last non-ambiguous customer language so
    # single-word replies ("yes", "COD", "1") stay in that language (migration 0021)
    last_customer_language: Mapped[str] = mapped_column(
        String, default="english", nullable=False, server_default="english"
    )

    # Escalation tracking — incremented each time the escalation guard fires
    escalation_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    last_escalation_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="conversation", order_by="Message.created_at"
    )
