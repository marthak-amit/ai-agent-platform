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
    cached_tokens: int = 0
    audio_seconds: float = 0.0
    cost_usd: float = 0.0
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
    orders_created: int = 0                                  # non-sandbox orders created in the window
    cost_per_order_created_inr: Optional[float] = None       # total ₹ / orders_created (None when no order was created)
    cost_per_order_created_usd: Optional[float] = None


class LLMClientCost7d(BaseModel):
    """One client's LLM spend over the last 7 days against the orders its customers confirmed."""

    client_id: int
    business_name: Optional[str] = None
    llm_calls: int
    cost_inr: float
    cost_usd: float
    confirmed_orders: int                                   # placed in chat, not cancelled, not sandbox
    avg_inr_per_confirmed_order: Optional[float] = None     # None when the client had no confirmed order


class LLMCostReport(LLMUsageReport):
    """GET /admin/llm-cost response: the usage report with $ and ₹, the cost per order created and the per-client 7-day average."""

    per_client_last_7d: list[LLMClientCost7d] = []
