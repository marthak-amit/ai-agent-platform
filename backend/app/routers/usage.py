"""
Usage stats router.

Endpoints:
- GET /usage/stats — return today_count, monthly_count, limit, percentage_used
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import usage_service

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
