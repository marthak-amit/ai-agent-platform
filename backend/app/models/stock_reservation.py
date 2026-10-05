"""ORM model for stock reserved by an order that is awaiting payment."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

RESERVATION_ACTIVE = "active"
RESERVATION_CONSUMED = "consumed"
RESERVATION_RELEASED = "released"


class StockReservation(Base):
    """
    One row per (order line item → product/variant) reservation.

    Lifecycle: 'active' while the order is pending_payment/payment_submitted,
    'consumed' when the order is paid (stock is deducted in the same
    transaction), 'released' when it is cancelled. Available stock for new
    orders is stock minus the sum of ACTIVE reservations.
    """

    __tablename__ = "stock_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), nullable=False)
    variant_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("product_variants.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=RESERVATION_ACTIVE, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
