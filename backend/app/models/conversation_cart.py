"""ORM model for the agent-v3 order cart (one active row per conversation)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CART_ACTIVE = "active"
CART_CONFIRMED = "confirmed"
CART_CANCELLED = "cancelled"
CART_EXPIRED = "expired"


class ConversationCart(Base):
    """
    The single source of truth for "what is this customer buying" under agent v3 (migration 0072).

    Replaces the flat slots on ``conversations`` (selected_color, pending_order_quantity, payment_method …) for
    clients with ``agent_v3_enabled``. A conversation has at most ONE ``active`` cart (partial unique index); a
    confirmed / cancelled / expired cart stays as history and a fresh active cart is opened for the next purchase.

    lines    — ``[{line_id, sku, color, size, material, qty}]``. Several colours of one product are several lines.
               A field the customer has not given yet is ``null`` (never guessed).
    customer — ``{name, address, mobile}``.
    state    — planner-side memory that must survive turns: ``stage``, whether the summary is on screen
               (``summary_version``), viewed / offered SKUs (upsell exclusions), the last clarify question and
               per-slot failed-clarify counters, orders already acknowledged. Never read by customers' templates directly.
    version  — bumped on every committed change; a confirmation is only valid for the version whose summary was shown.
    """

    __tablename__ = "conversation_cart"
    __table_args__ = (
        Index(
            "uq_conversation_cart_active", "conversation_id", unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_conversation_cart_conversation_status", "conversation_id", "status"),      # migration 0076
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    lines: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    customer: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    payment_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=CART_ACTIVE)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
