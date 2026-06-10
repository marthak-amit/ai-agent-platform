"""ORM model for the per-client knowledge base."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeBase(Base):
    """
    One row per Q&A entry per client.

    Entries can be created manually by the client via the dashboard,
    or auto-learned each week from frequently-asked customer questions.
    Auto-learned entries are approved by default but can be disabled.
    """

    __tablename__ = "knowledge_base"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=False, index=True
    )

    question: Mapped[str] = mapped_column(String, nullable=False)
    answer: Mapped[str] = mapped_column(String, nullable=False)

    # manual / auto_learned / pdf_upload
    source: Mapped[str] = mapped_column(String, default="manual", nullable=False)

    # delivery / product / payment / policy / general
    category: Mapped[str | None] = mapped_column(String, nullable=True)

    language: Mapped[str] = mapped_column(String, default="english", nullable=False)

    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    helpful_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # auto_learned entries start approved; client can flip either flag
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow, nullable=False
    )
