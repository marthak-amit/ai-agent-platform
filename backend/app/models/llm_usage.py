"""ORM model for one persisted LLM (or STT) call: tokens, cost, latency, outcome."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LLMUsage(Base):
    """
    One row per provider call, written by app.services.llm_usage_service.

    client_id / conversation_id / order_id are deliberately plain indexed integers
    (no foreign keys): usage is cost telemetry that must survive deleting or
    resetting a conversation/order. order_id is NULL while the conversation is
    still browsing and is back-filled for the conversation's unattributed rows
    when its order is created.
    """

    __tablename__ = "llm_usage"
    __table_args__ = (
        Index("ix_llm_usage_client_created", "client_id", "created_at"),
        Index("ix_llm_usage_conversation", "conversation_id"),
        Index("ix_llm_usage_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
