"""ORM model for a pre-built photo-enhancement style reference."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

VALID_STYLE_TYPES = {"dummy", "human_model", "hanging"}


class StyleReference(Base):
    """
    One row per pre-built style photo a seller can apply to a raw product image.

    Shared across all clients (not tenant-scoped) — this is a platform-curated
    library, filtered by product category (e.g. "saree") and style_type when
    shown in the dashboard's style picker.
    """

    __tablename__ = "style_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    style_type: Mapped[str] = mapped_column(String, nullable=False)
    reference_image_url: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
