"""ORM model for a product variant (colour/size/material combination)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProductVariant(Base):
    """
    One row per sellable variant of a product (e.g. a specific colour/size combo).

    Linked to the owning product via product_id and to the owning client via
    client_id. Used by catalogue_service to surface in-stock variant options.
    """

    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    material: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sku: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship("Product", back_populates="variants")
