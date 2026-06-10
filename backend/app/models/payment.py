"""ORM model for a Razorpay QR code payment."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Payment(Base):
    """
    One row per Razorpay QR code created.

    status transitions: created → paid (on Razorpay webhook) or cancelled.
    amount is stored in paise (1 INR = 100 paise).
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, index=True)
    qr_code_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="created")
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    customer_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
