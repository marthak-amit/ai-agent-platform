"""ORM model for a client's product catalogue entry."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

_UNSET = object()  # sentinel — distinguishes "not provided" from False/0/None


class Product(Base):
    """
    One row per product in a client's catalogue.

    Linked to the owning client via client_id. Used by catalogue_service to
    search and inject relevant product context into AI prompts.

    New fields added in migration 0008:
      sku, category, is_active, low_stock_alert
    """

    __tablename__ = "products"
    __table_args__ = (Index("ix_products_client_active", "client_id", "is_active"),)      # migration 0076

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Enhanced catalogue fields (migration 0008)
    sku: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    low_stock_alert: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Sample row seeded for a new account (migration 0070); removed in one click via "Remove samples".
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # Per-product delivery override (migration 0036); None → use client default
    delivery_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    variants: Mapped[list["ProductVariant"]] = relationship(
        "ProductVariant", back_populates="product", lazy="selectin"
    )
