"""ORM model for a product stock adjustment event."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

VALID_REASONS = {"sold", "restocked", "correction", "damaged"}


class StockLog(Base):
    """
    One row per stock adjustment for a product.

    Created by catalogue_service.adjust_stock() every time a manual or
    system-triggered stock change occurs. Provides an audit trail for
    inventory management shown in the product edit modal.

    reason must be one of: sold, restocked, correction, damaged.
    adjustment is negative for reductions, positive for additions.
    """

    __tablename__ = "stock_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    adjustment: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="correction")
    stock_before: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
