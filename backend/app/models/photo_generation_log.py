"""ORM model for a product-photo-enhancement generation event (COGS tracking)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PhotoGenerationLog(Base):
    """
    One row per Gemini photo-enhancement API call, successful or not.

    Used to track image-generation COGS per client. client_id is denormalised
    onto the row (rather than joined via product) so cost reporting never
    needs to join through products/variants.
    """

    __tablename__ = "photo_generation_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id"), nullable=False, index=True
    )
    variant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("product_variants.id"), nullable=False, index=True
    )
    style_reference_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("style_references.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    cost_estimate_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
