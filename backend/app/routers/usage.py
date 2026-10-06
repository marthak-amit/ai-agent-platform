"""
Usage stats router.

Endpoints:
- GET /usage/stats — return today_count, monthly_count, limit, percentage_used
- GET /usage/llm   — the caller's LLM token/cost rollup (per day/purpose/model/conversation/order).
                     The admin equivalent (all clients, optional client_id) is GET /admin/usage/llm.
"""

import logging
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_owner_client as get_current_client
from app.schemas.llm_usage import LLMUsageReport
from app.services import llm_usage_service, usage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/usage", tags=["usage"])


class UsageStatsOut(BaseModel):
    """Usage statistics for the authenticated client."""

    today_count: int
    monthly_count: int
    limit: int
    percentage_used: float


@router.get("/stats", response_model=UsageStatsOut)
async def get_usage_stats(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> UsageStatsOut:
    """
    Return daily and monthly message usage for the authenticated client.

    Args:
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        UsageStatsOut with today_count, monthly_count, limit, percentage_used.
    """
    stats = await usage_service.get_stats(db, current_client)
    return UsageStatsOut(**stats)


@router.get("/llm", response_model=LLMUsageReport)
async def get_llm_usage(
    current_client: Annotated[Client, Depends(get_current_client)],
    from_date: Optional[date] = Query(None, alias="from", description="Inclusive start date (default: 29 days before `to`)."),
    to_date: Optional[date] = Query(None, alias="to", description="Inclusive end date (default: today)."),
    limit: int = Query(100, ge=1, le=500, description="Max rows in per_conversation / per_order."),
    db: AsyncSession = Depends(get_db),
) -> LLMUsageReport:
    """
    LLM tokens, ₹ cost, latency and failures for the authenticated client, from the llm_usage ledger.

    Returns totals plus rollups per day, purpose, model, conversation and order.
    """
    report = await llm_usage_service.usage_report(db, client_id=current_client.id, start=from_date, end=to_date, limit=limit)
    return LLMUsageReport(client_id=current_client.id, **report)
