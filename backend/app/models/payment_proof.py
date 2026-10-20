"""ORM model for a customer-submitted payment screenshot awaiting seller review."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROOF_PENDING = "pending"
PROOF_APPROVED = "approved"
PROOF_REJECTED = "rejected"
PROOF_WITHDRAWN = "withdrawn"      # the customer said the image was not a payment proof (a product photo / the wrong picture)


class PaymentProof(Base):
    """
    One row per payment screenshot the customer sent while an order was awaiting payment.

    status: 'pending' | 'approved' | 'rejected' | 'withdrawn' (the customer took it back; kept for the audit trail,
    never shown in the seller's pending queue). Several proofs can exist for
    one order (re-sends after a rejection, or extra images while under review);
    the seller's approve/reject decision applies to every still-pending proof
    of that order. The image itself is stored in OUR storage (media_url) —
    Meta's media URLs expire — and is never run through a vision model.
    """

    __tablename__ = "payment_proofs"
    __table_args__ = (Index("ix_payment_proofs_conversation_status", "conversation_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=True
    )
    message_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("messages.id"), nullable=True)
    media_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=PROOF_PENDING, server_default="pending")
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_by_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The customer said "this is the payment" about an image we had first taken for something else (migration 0075).
    reclassified_by_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
