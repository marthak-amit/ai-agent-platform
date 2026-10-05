"""ORM model for a customer-submitted payment screenshot awaiting seller review."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROOF_PENDING = "pending"
PROOF_APPROVED = "approved"
PROOF_REJECTED = "rejected"


class PaymentProof(Base):
    """
    One row per payment screenshot the customer sent while an order was awaiting payment.

    status: 'pending' | 'approved' | 'rejected'. Several proofs can exist for
    one order (re-sends after a rejection, or extra images while under review);
    the seller's approve/reject decision applies to every still-pending proof
    of that order. The image itself is stored in OUR storage (media_url) —
    Meta's media URLs expire — and is never run through a vision model.
    """

    __tablename__ = "payment_proofs"

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
