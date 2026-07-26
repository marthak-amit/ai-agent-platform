"""ORM model for a persisted LLM cost-log entry."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CostLogEntry(Base):
    """
    One row per message/LLM-call entry recorded by app.services.cost_log.

    Mirrors the in-memory entry shape used by cost_log.log() — direction,
    path, model, token counts, cost, and call_kind — so persistence doesn't
    change what's tracked, only where it's kept.
    """

    __tablename__ = "cost_log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    path: Mapped[str] = mapped_column(String, nullable=False, default="TEMPLATE")
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    in_tok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_tok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    call_kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
