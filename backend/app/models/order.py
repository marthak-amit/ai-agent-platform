"""ORM model for a customer order."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.order_line_item import OrderLineItem


class Order(Base):
    """
    One row per customer order — the cart/purchase header (Phase 1 cart engine).

    order_number is auto-generated in the format ORD-YYYY-NNNN (per client),
    one per checkout regardless of how many line items it contains.
    payment_method: 'COD' or 'UPI'.
    payment_status: 'pending' / 'paid' / 'failed'.
    status: 'new' / 'pending_payment' / 'payment_submitted' / 'confirmed' / 'paid' / 'processing' / 'dispatched' / 'delivered' / 'cancelled'.

    line_items holds the full cart (see app.models.order_line_item.OrderLineItem).
    The flat product_name/product_sku/variant_color/variant_size/variant_material/
    quantity/unit_price columns below are a denormalized copy of line_items[0],
    kept so the seller dashboard and CSV export (which read these flat columns
    directly and are out of scope for the Phase 1 cart rewrite) keep working
    unchanged for multi-item carts too. total_amount is the CART GRAND TOTAL
    across all line items, not just line item 1 — see order_service.create_cart_order().
    """

    __tablename__ = "orders"
    __table_args__ = (
        # The agent looks up "this customer's unpaid orders" on every message (by conversation, and by phone for another channel):
        # tiny partial indexes over the two payment-open states instead of a scan of the tenant's whole order history.
        Index("ix_orders_open_payment_conversation", "client_id", "conversation_id", postgresql_where=text("status IN ('pending_payment','payment_submitted')")),
        Index("ix_orders_open_payment_phone", "client_id", "customer_phone", postgresql_where=text("status IN ('pending_payment','payment_submitted')")),
        Index("ix_orders_conversation_id", "conversation_id"),
        Index("ix_orders_client_conversation_status", "client_id", "conversation_id", "status"),      # migration 0076
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String, unique=True, index=True)

    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("conversations.id"), nullable=True)

    # Customer details
    customer_name: Mapped[str] = mapped_column(String, nullable=False)
    customer_phone: Mapped[str] = mapped_column(String, nullable=False, index=True)
    delivery_address: Mapped[str] = mapped_column(Text, nullable=False)

    # Delivery contact number — NOT identity. WhatsApp: same as customer_phone
    # (auto-filled from sender). Instagram: collected as an order slot since
    # customer_phone there is the IGSID, not a real phone (migration 0048).
    mobile_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Order details
    product_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    product_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_size: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    variant_material: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
    payment_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Placed by the in-dashboard simulator (migration 0070): never listed, counted, reserved, notified or invoiced.
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # High-value guard (migration 0071): total above the client's threshold → the payment may only be approved after
    # the seller acknowledges the manual review (payment_verification_service.approve_payment).
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    manual_review_cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stock guard — prevents double-deduction if the order is processed twice
    stock_deducted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Idempotency key — set to the triggering WhatsApp message ID (wamid).
    # Prevents double-insert when Meta retries the webhook for the same message.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(250), nullable=True, unique=True)

    # Notes / invoice
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invoice_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Cart line items (migration 0058). lazy="selectin" so accessing this
    # relationship from an async context never triggers a lazy-load / MissingGreenlet.
    line_items: Mapped[list["OrderLineItem"]] = relationship(
        "OrderLineItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderLineItem.line_number",
        lazy="selectin",
    )
