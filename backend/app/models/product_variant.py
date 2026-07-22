"""ORM model for a product variant (colour/size/material combination)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

VALID_ENHANCED_STATUSES = {"pending", "done", "failed", "flagged_color_mismatch"}


class ProductVariant(Base):
    """
    One row per sellable variant of a product (e.g. a specific colour/size combo).

    Linked to the owning product via product_id and to the owning client via
    client_id. Used by catalogue_service to surface in-stock variant options.

    Photo-enhancement fields (migration 0051): image_url always holds the
    seller's original raw uploaded photo (the Gemini fusion input) and is
    never overwritten by generation. enhanced_image_url/enhanced_status hold
    the latest Gemini output and its QC state; enhanced_approved is the
    seller's manual publish gate — display_image_url is the only field public
    surfaces (storefront/catalog) should read.
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

    # Photo enhancement (migration 0051)
    enhanced_image_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    enhanced_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    style_reference_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("style_references.id"), nullable=True
    )
    enhanced_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship("Product", back_populates="variants")

    @property
    def display_image_url(self) -> Optional[str]:
        """Public-facing image: the approved enhanced photo, else the raw upload."""
        if self.enhanced_status == "done" and self.enhanced_approved and self.enhanced_image_url:
            return self.enhanced_image_url
        return self.image_url
