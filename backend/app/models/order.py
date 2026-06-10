"""ORM model for a customer order."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Order(Base):
    """
    One row per customer order.

    order_number is auto-generated in the format ORD-YYYY-NNNN (per client).
    payment_method: 'COD' or 'UPI'.
    payment_status: 'pending' / 'paid' / 'failed'.
    status: 'new' / 'confirmed' / 'paid' / 'processing' / 'dispatched' / 'delivered' / 'cancelled'.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String, unique=True, index=True)

    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("conversations.id"), nullable=True)

    # Customer details
    customer_name: Mapped[str] = mapped_column(String, nullable=False)
    customer_phone: Mapped[str] = mapped_column(String, nullable=False, index=True)
    delivery_address: Mapped[str] = mapped_column(Text, nullable=False)

    # Order details
    product_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    product_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_size: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)

    # Payment
    payment_method: Mapped[str] = mapped_column(String, default="COD")
    payment_status: Mapped[str] = mapped_column(String, default="pending")
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Order status
    status: Mapped[str] = mapped_column(String, default="new", index=True)

    # Tracking
    tracking_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    courier_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stock guard — prevents double-deduction if the order is processed twice
    stock_deducted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Notes / invoice
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invoice_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
