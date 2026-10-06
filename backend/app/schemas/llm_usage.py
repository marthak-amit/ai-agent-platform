"""Response schemas for the LLM usage / cost report endpoints."""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class LLMUsageMetrics(BaseModel):
    """Aggregate figures shared by every rollup row."""

    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_inr: float
    failures: int
    avg_latency_ms: float


class LLMUsageByDay(LLMUsageMetrics):
    """One calendar day (in the report timezone)."""

    day: date


class LLMUsageByPurpose(LLMUsageMetrics):
    """One call purpose (reply, classify_intent, vision, stt, ...)."""

    purpose: str


class LLMUsageByModel(LLMUsageMetrics):
    """One model id."""

    model: str


class LLMUsageByConversation(LLMUsageMetrics):
    """One conversation (most expensive first)."""

    conversation_id: int


class LLMUsageByOrder(LLMUsageMetrics):
    """One order (most expensive first); order_number is None if the order row is gone."""

    order_id: int
    order_number: Optional[str] = None


class LLMUsageReport(BaseModel):
    """GET /usage/llm response."""

    from_date: date
    to_date: date
    timezone: str
    client_id: Optional[int] = None  # None = all clients (admin)
    totals: LLMUsageMetrics
    per_day: list[LLMUsageByDay]
    per_purpose: list[LLMUsageByPurpose]
    per_model: list[LLMUsageByModel]
    per_conversation: list[LLMUsageByConversation]
    per_order: list[LLMUsageByOrder]
