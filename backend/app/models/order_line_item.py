"""ORM model for one line item within a cart-based order."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.order import Order


class OrderLineItem(Base):
    """
    One row per product/variant within a single Order (cart/purchase header).

    A simple single-product order still gets exactly one OrderLineItem row —
    see order_service.create_cart_order(). subtotal = quantity * unit_price.
    """

    __tablename__ = "order_line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )

    product_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    product_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_size: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_material: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False)

    # 1-indexed position within the cart, in the order items were added.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped["Order"] = relationship("Order", back_populates="line_items")
