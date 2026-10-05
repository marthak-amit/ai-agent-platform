"""
Self-serve channel-connection OAuth router.

Endpoints:
- GET  /integrations/instagram/connect    : build the Meta OAuth authorize URL
- GET  /integrations/instagram/callback   : exchange code for a token, store on the client
- POST /integrations/instagram/disconnect : clear stored Instagram credentials

Each client connects their OWN Instagram Business Account through a Facebook
Page (Facebook Login for Business flow). The `state` query param is a signed,
short-lived JWT binding the callback to the client_id that started the flow,
so the callback cannot be replayed against a different tenant.
"""

import logging
from typing import Annotated, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_owner_client as get_current_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations/instagram", tags=["integrations"])

META_API_VERSION = "v21.0"
META_OAUTH_AUTHORIZE_URL = "https://www.facebook.com/v21.0/dialog/oauth"
META_GRAPH_BASE_URL = "https://graph.facebook.com"
OAUTH_SCOPES = "instagram_basic,instagram_manage_messages,pages_show_list,pages_manage_metadata"

STATE_ALGORITHM = "HS256"
STATE_EXPIRE_SECONDS = 600  # 10 minutes — long enough to complete the Meta consent screen

# Where the callback sends the browser back to. Whitelisted (not an arbitrary
# URL) so the OAuth state can't be abused as an open redirect.
RETURN_PATHS = {"channels": "/channels", "onboarding": "/onboarding"}
DEFAULT_RETURN_TO = "channels"


def _create_state_token(client_id: int, return_to: str) -> str:
    """Sign a short-lived state token binding the OAuth flow to one client_id."""
    from datetime import datetime, timedelta, timezone

    settings = get_settings()
    payload = {
        "client_id": client_id,
        "return_to": return_to,
        "purpose": "ig_oauth_state",
        "exp": datetime.now(timezone.utc) + timedelta(seconds=STATE_EXPIRE_SECONDS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=STATE_ALGORITHM)


def _verify_state_token(state: str) -> tuple[int, str]:
    """
    Decode and validate the OAuth state token.

    Returns:
        (client_id, return_to) — return_to is always a valid RETURN_PATHS key.

    Raises:
        HTTPException 400: If the state is missing, expired, malformed, or
            was not issued for this purpose.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=[STATE_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state. Please restart the connection.",
        ) from exc

    if payload.get("purpose") != "ig_oauth_state" or "client_id" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state.",
        )
    return_to = payload.get("return_to")
    if return_to not in RETURN_PATHS:
        return_to = DEFAULT_RETURN_TO
    return int(payload["client_id"]), return_to


@router.get("/connect")
async def connect_instagram(
    current_client: Annotated[Client, Depends(get_current_client)],
    return_to: str = Query(default=DEFAULT_RETURN_TO),
) -> dict:
    """
    Build the Meta OAuth authorize URL for the current client to connect Instagram.

    Args:
        return_to: Where the OAuth callback should send the browser back to
            once the flow completes — "channels" (default, the standalone
            Settings → Channels page) or "onboarding" (mid-wizard). Any other
            value falls back to "channels".

    Returns:
        {"url": "<authorize url>"} — the frontend redirects the browser there.

    Raises:
        HTTPException 400: If META_APP_ID or the redirect URI is not configured.
    """
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_oauth_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Instagram connection is not configured on this server yet.",
        )

    if return_to not in RETURN_PATHS:
        return_to = DEFAULT_RETURN_TO
    state = _create_state_token(current_client.id, return_to)
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_oauth_redirect_uri,
        "scope": OAUTH_SCOPES,
        "state": state,
        "response_type": "code",
    }
    url = f"{META_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
    return {"url": url}


@router.get("/callback")
async def instagram_callback(
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
) -> RedirectResponse:
    """
    Handle the Meta OAuth redirect: verify state, exchange the code for a
    long-lived token, resolve the connected Instagram Business Account ID,
    and store both on the client that started the flow.

    Returns:
        Redirect to wherever the flow was started from (Settings → Channels,
        or mid-onboarding) with a status query param (connected | cancelled |
        error | no_ig_account) instead of a raw 500, since this is a
        browser-facing redirect endpoint. Falls back to /channels when the
        state can't be decoded (e.g. it expired) since there's no return_to
        to recover at that point.
    """
    settings = get_settings()
    fallback_url = f"{settings.frontend_url}{RETURN_PATHS[DEFAULT_RETURN_TO]}"

    if error:
        logger.info("Instagram OAuth cancelled by user: %s", error)
        return RedirectResponse(f"{fallback_url}?ig_status=cancelled")

    if not code or not state:
        return RedirectResponse(f"{fallback_url}?ig_status=error")

    try:
        client_id, return_to = _verify_state_token(state)
    except HTTPException:
        logger.warning("Instagram OAuth callback with invalid/replayed state.")
        return RedirectResponse(f"{fallback_url}?ig_status=error")

    frontend_settings_url = f"{settings.frontend_url}{RETURN_PATHS[return_to]}"

    client = await db.get(Client, client_id)
    if client is None:
        return RedirectResponse(f"{frontend_settings_url}?ig_status=error")

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            # 1. Exchange the authorization code for a short-lived user token.
            short_lived_resp = await http.get(
                f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/oauth/access_token",
                params={
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "redirect_uri": settings.meta_oauth_redirect_uri,
                    "code": code,
                },
            )
            short_lived_resp.raise_for_status()
            short_lived_token = short_lived_resp.json()["access_token"]

            # 2. Exchange the short-lived token for a long-lived one (~60 days).
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

            # 3. Find the Page(s) this user manages, then the IG account linked to one.
            pages_resp = await http.get(
                f"{META_GRAPH_BASE_URL}/{META_API_VERSION}/me/accounts",
                params={
                    "access_token": long_lived_token,
                    "fields": "instagram_business_account",
                },
            )
            pages_resp.raise_for_status()
            pages = pages_resp.json().get("data", [])
    except httpx.HTTPStatusError as exc:
        logger.error("Instagram OAuth token exchange failed: %s", exc.response.text)
        return RedirectResponse(f"{frontend_settings_url}?ig_status=error")
    except httpx.RequestError as exc:
        logger.error("Instagram OAuth network error: %s", exc)
        return RedirectResponse(f"{frontend_settings_url}?ig_status=error")

    instagram_account_id = None
    for page in pages:
        ig_account = page.get("instagram_business_account")
        if ig_account and ig_account.get("id"):
            instagram_account_id = ig_account["id"]
            break

    if instagram_account_id is None:
        logger.info("Instagram OAuth: client %s has no linked IG business account.", client_id)
        return RedirectResponse(f"{frontend_settings_url}?ig_status=no_ig_account")

    client.instagram_access_token = long_lived_token
    client.instagram_account_id = instagram_account_id
    await db.commit()

    return RedirectResponse(f"{frontend_settings_url}?ig_status=connected")


@router.post("/disconnect", status_code=status.HTTP_200_OK)
async def disconnect_instagram(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Disconnect Instagram for the current client by clearing stored credentials.

    Returns:
        {"success": True}
    """
    current_client.instagram_access_token = None
    current_client.instagram_account_id = None
    await db.commit()
    return {"success": True}
