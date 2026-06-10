"""
Daily briefing router.

Handles:
- POST /briefing/send-now : send today's briefing immediately (test / manual trigger)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.auth import get_current_client
from app.models.client import Client
from app.services import briefing_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.post("/send-now", status_code=status.HTTP_200_OK)
async def send_briefing_now(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Immediately generate and send today's briefing to the logged-in client.

    Used for testing without waiting for the 9AM scheduled job.

    Args:
        current_client: Authenticated client from JWT.
        db:             Injected async DB session.

    Returns:
        {"status": "sent", "preview": "<briefing text>"} on success.

    Raises:
        HTTPException 400: If no owner phone is configured.
        HTTPException 502: If WhatsApp delivery fails.
    """
    if not current_client.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No owner phone number configured. Add your phone number in Settings first.",
        )

    briefing = await briefing_service.generate_daily_briefing(current_client.id, db)

    try:
        from app.services import whatsapp_service
        await whatsapp_service.send_text_message(
            to_phone_number=current_client.phone,
            message_text=briefing,
        )
    except Exception as exc:
        logger.error("send-now WhatsApp delivery failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"WhatsApp delivery failed: {exc}",
        ) from exc

    return {"status": "sent", "preview": briefing}
