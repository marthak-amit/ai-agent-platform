"""ORM model for a customer conversation thread."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    __table_args__ = (
        UniqueConstraint(
            "client_id", "channel", "phone_number",
            name="uq_conversations_client_channel_phone",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String, default="whatsapp")

    # Tenant owner — NOT NULL + part of the composite unique key above
    # (client_id, channel, phone_number) since the contract stage of the
    # identity migration (migration 0047).
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )

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

    # Delivery contact number — NOT identity (identity key is phone_number above).
    # WhatsApp: auto-filled from the sender's WA number. Instagram: collected as
    # an order slot since the IGSID is not a phone number (migration 0048).
    mobile_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)

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

    # Last product card shown to the customer — survives the post-order reset
    # of pending_product_sku so a bare "yes" after order completion can be
    # repinned deterministically instead of falling through to the LLM
    # (migration 0043).
    last_shown_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # JSON list of SKUs shown together in a still-open "which one?" multi-option
    # list (e.g. two name-matched products). Non-empty means a CHOICE is pending —
    # a bare affirmative ("Yes") is not a valid answer and must be rejected/re-asked
    # rather than repinned from last_shown_sku (migration 0044).
    pending_choice_skus: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Browsed SKUs — JSON list of SKUs the customer showed buying intent for this conversation.
    # Appended on every product switch/pin; used for end-of-order cross-sell.
    browsed_skus: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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

    # Abandoned-intent follow-up tracking (migration 0035)
    # last_followup_sku:  SKU for which the most recent follow-up was sent;
    #                     prevents duplicate follow-ups for the same product.
    # followup_sent_at:   Timestamp of that send; enforces 7-day global cooldown.
    last_followup_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    followup_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Per-conversation button nonce — rotated every time interactive buttons are sent.
    # Button IDs are encoded as "{action}~{conv_id}~{nonce}"; a tap with a mismatched
    # nonce is stale (old button) and is rejected without side effects. (migration 0041)
    current_button_nonce: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Improvement 1: per-slot failed-attempt counter (migration 0042)
    slot_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    slot_attempt_slot: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # which slot is being counted

    # Improvement 2: off-topic abuse counter (migration 0042)
    off_topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")

    # Improvement 3: per-phone/day LLM budget (migration 0042)
    llm_calls_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    llm_calls_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # UTC date "YYYY-MM-DD"

    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="conversation", order_by="Message.created_at"
    )
