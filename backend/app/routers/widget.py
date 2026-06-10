"""
Website chat widget router.

Endpoints:
- POST /widget/message          : customer sends a message, returns AI reply
- GET  /widget/config/{api_key} : returns branding config for the widget UI

Auth: api_key in request body (not JWT). Requires pro plan.

Message flow mirrors the WhatsApp webhook pipeline:
  1. Validate api_key → resolve client
  2. Check plan allows "website" channel
  3. Load / create conversation keyed on session_id
  4. Build Gemini context (history + system prompt + catalogue)
  5. Generate reply
  6. Persist messages, record usage, tag lead
  7. Return reply text
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.services import (
    catalogue_service,
    conversation_service,
    gemini_service,
    lead_service,
    plan_service,
    usage_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/widget", tags=["widget"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class WidgetMessageRequest(BaseModel):
    """Request body for POST /widget/message."""

    api_key: str
    session_id: str
    message: str


class WidgetMessageResponse(BaseModel):
    """Response for POST /widget/message."""

    reply: str
    session_id: str


class WidgetConfigResponse(BaseModel):
    """Branding config returned to the widget on load."""

    business_name: str
    welcome_message: str
    brand_color: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_client_by_api_key(db: AsyncSession, api_key: str) -> Client:
    """
    Look up an active client by their API key.

    Args:
        db:      Active async DB session.
        api_key: The vp_-prefixed key issued during onboarding.

    Returns:
        Matching Client instance.

    Raises:
        HTTPException 401: If the key does not match any active client.
    """
    result = await db.execute(
        select(Client).where(
            Client.api_key == api_key,
            Client.is_active == True,  # noqa: E712
        )
    )
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key.",
        )
    return client


def _require_website_plan(client: Client) -> None:
    """
    Raise 403 if the client's plan does not include the website channel.

    The website widget is available on the Pro plan only.

    Args:
        client: Resolved Client instance.

    Raises:
        HTTPException 403: If plan does not allow "website".
    """
    if not plan_service.plan_allows_channel(client.plan_slug, "website"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Plan '{client.plan_slug}' does not include the website widget. "
                "Upgrade to Pro to enable it."
            ),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/message",
    response_model=WidgetMessageResponse,
    status_code=status.HTTP_200_OK,
)
async def widget_message(
    body: WidgetMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> WidgetMessageResponse:
    """
    Process a customer message from the embedded website widget.

    Uses the same Gemini pipeline as the WhatsApp webhook: conversation
    history, system prompt, and catalogue context are all included.

    Args:
        body: api_key, session_id (UUID from the widget), message text.
        db:   Injected async DB session.

    Returns:
        WidgetMessageResponse with the AI reply and the session_id echoed back.

    Raises:
        HTTPException 401: If api_key is invalid.
        HTTPException 403: If the client's plan does not include website.
    """
    client = await _get_client_by_api_key(db, body.api_key)
    _require_website_plan(client)

    session_id = body.session_id or str(uuid.uuid4())

    conv = await conversation_service.get_or_create_conversation(
        db, session_id, channel="website"
    )
    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    products = await catalogue_service.list_products(db, client.id)
    relevant = catalogue_service.search_products(products, body.message)
    catalogue_context = (
        catalogue_service.format_catalogue_context(relevant) if relevant else None
    )

    try:
        ai_reply = await gemini_service.generate_reply(
            body.message,
            history=history_dicts,
            system_prompt=client.gemini_system_prompt,
            catalogue_context=catalogue_context,
        )
    except Exception as exc:
        logger.error("Gemini error on widget message: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service temporarily unavailable. Please try again.",
        ) from exc

    await conversation_service.save_message(db, conv.id, "user", body.message)
    await conversation_service.save_message(db, conv.id, "model", ai_reply)

    try:
        await usage_service.record_message(db, client)
    except Exception as exc:
        logger.error("Usage tracking error on widget: %s", exc)

    all_messages = history_dicts + [
        {"role": "user", "content": body.message},
        {"role": "model", "content": ai_reply},
    ]
    try:
        await lead_service.tag_lead(db, session_id, conv.id, all_messages)
    except Exception as exc:
        logger.error("Lead tagging error on widget: %s", exc)

    return WidgetMessageResponse(reply=ai_reply, session_id=session_id)


@router.get(
    "/config/{api_key}",
    response_model=WidgetConfigResponse,
    status_code=status.HTTP_200_OK,
)
async def widget_config(
    api_key: str,
    db: AsyncSession = Depends(get_db),
) -> WidgetConfigResponse:
    """
    Return public branding configuration for the embedded widget.

    Called by the widget JavaScript on page load to customise the UI
    with the client's business name and welcome message.

    Args:
        api_key: The vp_-prefixed key from the script tag's data-api-key.
        db:      Injected async DB session.

    Returns:
        WidgetConfigResponse with business_name, welcome_message, brand_color.

    Raises:
        HTTPException 404: If the api_key does not match any active client.
    """
    result = await db.execute(
        select(Client).where(
            Client.api_key == api_key,
            Client.is_active == True,  # noqa: E712
        )
    )
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found.",
        )

    business_name = client.business_name or "AI Assistant"
    welcome_message = (
        f"Hi! Welcome to {business_name}. How can I help you today?"
    )

    return WidgetConfigResponse(
        business_name=business_name,
        welcome_message=welcome_message,
        brand_color="#6366f1",
    )
