"""
Self-serve WhatsApp connection via Meta's Embedded Signup (Tech Provider flow).

Endpoints:
- GET  /integrations/whatsapp/signup-config     : app_id/config_id the frontend
                                                    needs to launch FB.login()
- POST /integrations/whatsapp/embedded-signup   : exchange the code returned by
                                                    FB.login() for a token, store
                                                    it on the client, and register
                                                    the phone number with Meta

Unlike the Instagram connect flow (a browser redirect through Meta's OAuth
dialog), Embedded Signup runs inside a Facebook Login popup driven by the
Facebook JS SDK on the frontend (see docs.facebook.com/whatsapp/embedded-signup).
The popup itself walks the client through picking/creating a WhatsApp Business
Account and a phone number, and hands back an authorization `code` plus the
resulting `waba_id`/`phone_number_id` — no server-side redirect is involved,
so there's no OAuth `state` token here.

Requires a WhatsApp Embedded Signup "Configuration" to be created in the Meta
App Dashboard (App → WhatsApp → Embedded Signup → Configurations) and its ID
set as META_WHATSAPP_CONFIG_ID — see CLAUDE.md. Until that's configured,
signup-config reports enabled=False and the frontend falls back to manual
phone_number_id/access_token entry.
"""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_owner_client as get_current_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations/whatsapp", tags=["integrations"])

META_API_VERSION = "v21.0"
META_GRAPH_BASE_URL = "https://graph.facebook.com"


class SignupConfigOut(BaseModel):
    """Response for GET /integrations/whatsapp/signup-config."""

    enabled: bool
    app_id: str = ""
    config_id: str = ""


@router.get("/signup-config", response_model=SignupConfigOut)
async def get_signup_config(
    _current_client: Annotated[Client, Depends(get_current_client)],
) -> SignupConfigOut:
    """
    Report whether Embedded Signup is configured, and the app_id/config_id
    the frontend needs to call FB.login() with.

    Returns:
        {enabled: False} when META_WHATSAPP_CONFIG_ID isn't set — the
        frontend should fall back to manual credential entry in that case.
    """
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_whatsapp_config_id:
        return SignupConfigOut(enabled=False)
    return SignupConfigOut(
        enabled=True,
        app_id=settings.meta_app_id,
        config_id=settings.meta_whatsapp_config_id,
    )


class EmbeddedSignupRequest(BaseModel):
    """Request body for POST /integrations/whatsapp/embedded-signup."""

    code: str
    waba_id: str
    phone_number_id: str


class EmbeddedSignupResponse(BaseModel):
    """Response for POST /integrations/whatsapp/embedded-signup."""

    success: bool
    whatsapp_phone_number_id: str
    whatsapp_number: str | None = None


@router.post("/embedded-signup", response_model=EmbeddedSignupResponse)
async def complete_embedded_signup(
    body: EmbeddedSignupRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmbeddedSignupResponse:
    """
    Exchange the Embedded Signup authorization code for a token, subscribe
    this app to the client's WABA, and store the resulting credentials.

    Args:
        body:           {code, waba_id, phone_number_id} as returned by the
                         frontend's FB.login() callback + WA_EMBEDDED_SIGNUP
                         postMessage event.
        current_client: JWT-authenticated Client — credentials are stored on
                         this row, never on whichever client Meta's response
                         happens to mention.
        db:              Injected async DB session.

    Raises:
        HTTPException 400: If Embedded Signup isn't configured on this server.
        HTTPException 502: If any Meta Graph API call fails.
    """
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_whatsapp_config_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp Embedded Signup is not configured on this server yet.",
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            # 1. Exchange the authorization code for a short-lived token.
            #    Embedded Signup is a JS-SDK popup flow, not a redirect, so no
            #    redirect_uri is passed here (unlike the Instagram OAuth flow).
            short_lived_resp = await http.get(
                f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/oauth/access_token",
                params={
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "code": body.code,
                },
            )
            short_lived_resp.raise_for_status()
            short_lived_token = short_lived_resp.json()["access_token"]

            # 2. Exchange for a long-lived token (~60 days; Meta silently
            #    refreshes it on subsequent Graph API calls once it's a
            #    System User token for an approved Tech Provider app).
            long_lived_resp = await http.get(
                f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": short_lived_token,
                },
            )
            long_lived_resp.raise_for_status()
            long_lived_token = long_lived_resp.json()["access_token"]

            # 3. Subscribe our app to this WABA so we receive its webhooks
            #    (message/status callbacks) — required, not optional, for a
            #    Tech Provider managing multiple clients' WABAs.
            subscribe_resp = await http.post(
                f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/{body.waba_id}/subscribed_apps",
                params={"access_token": long_lived_token},
            )
            if not subscribe_resp.is_success:
                logger.error(
                    "WhatsApp Embedded Signup: subscribed_apps failed for waba=%s: %s",
                    body.waba_id,
                    subscribe_resp.text[:300],
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Could not subscribe to your WhatsApp Business Account. Please try again.",
                )

            # 4. Best-effort: fetch the display number for the UI. Not fatal —
            #    the phone_number_id alone is enough to send/receive messages.
            display_phone_number = None
            try:
                number_resp = await http.get(
                    f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/{body.phone_number_id}",
                    params={"access_token": long_lived_token, "fields": "display_phone_number"},
                )
                if number_resp.is_success:
                    display_phone_number = number_resp.json().get("display_phone_number")
            except httpx.RequestError:
                pass
    except httpx.HTTPStatusError as exc:
        logger.error("WhatsApp Embedded Signup token exchange failed: %s", exc.response.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Meta rejected the WhatsApp connection. Please try again.",
        ) from exc
    except httpx.RequestError as exc:
        logger.error("WhatsApp Embedded Signup network error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Meta. Check your connection and try again.",
        ) from exc

    current_client.whatsapp_access_token = long_lived_token
    current_client.whatsapp_phone_number_id = body.phone_number_id
    if display_phone_number:
        current_client.whatsapp_number = display_phone_number
    await db.commit()

    logger.info(
        "WhatsApp Embedded Signup complete for client=%s waba=%s phone_number_id=%s",
        current_client.id,
        body.waba_id,
        body.phone_number_id,
    )

    return EmbeddedSignupResponse(
        success=True,
        whatsapp_phone_number_id=body.phone_number_id,
        whatsapp_number=display_phone_number,
    )
