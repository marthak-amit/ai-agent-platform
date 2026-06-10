"""
Channels router.

Endpoints:
- POST /channels/test-whatsapp : send a test message to the owner's own number
"""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import get_settings
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])

META_API_VERSION = "v21.0"
META_API_BASE_URL = "https://graph.facebook.com"


@router.post("/test-whatsapp", status_code=status.HTTP_200_OK)
async def test_whatsapp(
    current_client: Annotated[Client, Depends(get_current_client)],
) -> dict:
    """
    Send a test WhatsApp message to the owner's registered phone number.

    Uses the client's stored whatsapp_phone_number_id and whatsapp_access_token
    if present, otherwise falls back to the global env-var credentials.

    Returns:
        {"success": True, "message": "Test message sent."} on success.

    Raises:
        HTTPException 400: If the client has no phone number on record.
        HTTPException 502: If the Meta API call fails.
    """
    phone = current_client.phone
    if not phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No phone number saved on your profile. Add one in Settings first.",
        )

    # Prefer per-client credentials; fall back to global env config.
    settings = get_settings()
    phone_number_id = current_client.whatsapp_phone_number_id or settings.whatsapp_phone_number_id
    access_token = current_client.whatsapp_access_token or settings.whatsapp_access_token

    if not phone_number_id or not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp credentials not configured. Save Phone Number ID and Access Token first.",
        )

    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    # Normalise phone: strip leading + so Meta gets E.164 digits only.
    to_phone = phone.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": "Hello! Your AI agent is connected and working correctly. This is a test message from your Vision+ dashboard.",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("WhatsApp test failed: %s", exc.response.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Meta API error: {exc.response.text}",
        ) from exc
    except httpx.RequestError as exc:
        logger.error("WhatsApp test network error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Meta API. Check your internet connection.",
        ) from exc

    return {"success": True, "message": "Test message sent to your number."}
