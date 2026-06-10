"""
Follow-up engine router.

Endpoints:
- POST /followup/run    : trigger a follow-up cycle (admin only)
- GET  /followup/stats  : delivery statistics (admin only)

Both endpoints require X-Admin-Key header.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.admin import require_admin
from app.services import followup_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/followup", tags=["followup"])


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_followups(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Trigger one complete follow-up cycle.

    Finds all warm/cold WhatsApp leads whose last message is older than 24 hours
    and who have not received a follow-up in the last 48 hours, generates a
    personalised Gemini message for each, sends it via WhatsApp, and records
    the attempt in the follow_ups table.

    Requires X-Admin-Key header.

    Args:
        db: Injected async DB session.

    Returns:
        Dict with sent, failed, and total_eligible counts.
    """
    result = await followup_service.send_followups(db)
    logger.info(
        "Follow-up run complete: %d sent, %d failed, %d eligible.",
        result["sent"], result["failed"], result["total_eligible"],
    )
    return result


@router.get("/stats", dependencies=[Depends(require_admin)])
async def followup_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Return follow-up delivery statistics.

    Requires X-Admin-Key header.

    Args:
        db: Injected async DB session.

    Returns:
        Dict with sent_today, sent_last_7_days, failed_last_7_days,
        and currently_eligible.
    """
    return await followup_service.get_stats(db)
