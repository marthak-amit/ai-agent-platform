"""
Instagram webhook router.

Handles:
- GET  /instagram  : webhook verification (same Meta challenge flow as WhatsApp)
- POST /instagram  : incoming DMs and post comments

Message types handled:
- Text/image/audio DMs: routed into the same channel-neutral order pipeline
  WhatsApp uses (handle_inbound_message) — slot machine, abuse protection,
  address-confirm gate, vision-based product matching, voice transcription,
  all apply identically to Instagram. See app/routers/_instagram_adapter.py
  for the reply send path.
- Post comments: comment → private-reply DM auto-trigger. A comment matching
  the client's configured keywords (or any comment, if reply-all is on) posts
  a brief public ack, then is fed into the SAME handle_inbound_message pipeline
  DMs use — the comment is just a new entry point, not a separate flow. The
  reply is sent via Meta's Private Reply API (send_private_reply), which can
  message a commenter with no prior DM thread; regular send_dm cannot.

Comment flow: see _handle_comment() for keyword-matching, dedup, and the
200/hour rate-limit queue.
"""

import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.models.ig_comment_reply import IgCommentReply
from app.models.message import Message as MessageModel
from app.routers._instagram_adapter import send_pipeline_result
from app.routers.auth import get_current_client
from app.schemas.instagram import InstagramMessaging, InstagramWebhookPayload
from app.schemas.webhook import AudioContent, ImageContent, TextContent, WhatsAppMessage
from app.services import (
    conversation_service,
    ig_comment_service,
    instagram_service,
    plan_service,
    vision_service,
)
from app.services.order_pipeline import InboundContext, handle_inbound_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instagram", tags=["instagram"])


@router.get("", response_class=PlainTextResponse)
async def verify_instagram_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> str:
    """
    Handle Meta webhook verification challenge for Instagram.

    Uses the same META_VERIFY_TOKEN as the WhatsApp webhook.

    Returns hub.challenge as plain text on success.

    Raises:
        HTTPException 403: If mode or token do not match.
    """
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        logger.info("Instagram webhook verification successful.")
        return hub_challenge
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")


def _verify_instagram_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Validate the X-Hub-Signature-256 header on Instagram webhooks.

    Meta signs Instagram webhooks with the same META_APP_SECRET and the same
    sha256= prefix as WhatsApp. An absent or malformed header returns False.

    Args:
        payload_bytes:    Raw bytes of the request body.
        signature_header: Value of the X-Hub-Signature-256 header.

    Returns:
        True if the computed HMAC-SHA256 matches the header, False otherwise.
    """
    settings = get_settings()
    if not signature_header.startswith("sha256="):
        return False
    expected = signature_header.removeprefix("sha256=")
    computed = hmac.new(
        key=settings.meta_app_secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)


async def _get_active_client(db: AsyncSession, instagram_account_id: str | None = None):
    """
    Return the active client matching the given Instagram Business Account ID.

    Looks up Client.instagram_account_id first (multi-tenant). Falls back to
    the first active client for single-tenant / development compatibility.

    Args:
        db:                    Active async DB session.
        instagram_account_id:  The IGBAID from webhook entry.id. May be None.

    Returns:
        Active Client ORM instance, or None.
    """
    if instagram_account_id:
        result = await db.execute(
            select(Client).where(
                Client.instagram_account_id == instagram_account_id,
                Client.is_active == True,  # noqa: E712
            ).limit(1)
        )
        client = result.scalar_one_or_none()
        if client:
            return client
        logger.warning(
            "No active client for instagram_account_id=%s — falling back to first active client.",
            instagram_account_id,
        )

    result = await db.execute(
        select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def _get_active_client_plan(db: AsyncSession) -> str:
    """
    Return the plan_slug of the first active client, defaulting to 'starter'.

    Used to gate Instagram processing before any payload parsing — so no
    instagram_account_id is available yet; falls back to the first active
    client like _get_active_client(db, None) already does.

    Kept as a standalone async helper so tests can patch it independently.
    """
    client = await _get_active_client(db, None)
    return (client.plan_slug if client else None) or "starter"


@router.post("", status_code=status.HTTP_200_OK)
async def receive_instagram_event(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle incoming Instagram DMs and post comments.

    Validates X-Hub-Signature-256 before processing anything.
    Requires growth or pro plan — starter plan returns plan_restricted.

    DM flow (text/image/audio): routed into handle_inbound_message() — the
                                 same order-pipeline orchestrator WhatsApp
                                 uses — and replied to via the IG adapter.
    Comment flow:    user comments → Gemini reply → public comment reply + DM.

    Always returns HTTP 200 to prevent Meta from retrying.

    Args:
        request: Raw FastAPI request (body bytes required for signature check).
        db:      Injected async DB session.

    Returns:
        {"status": "ok"} or {"status": "plan_restricted"} in all non-error cases.

    Raises:
        HTTPException 401: If X-Hub-Signature-256 is missing or invalid.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_instagram_signature(raw_body, signature):
        logger.warning("Invalid X-Hub-Signature-256 on Instagram webhook.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature.",
        )

    plan_slug = await _get_active_client_plan(db)
    if not await plan_service.plan_allows_channel(db, plan_slug, "instagram"):
        logger.info(
            "Instagram webhook skipped: plan '%s' does not include Instagram.", plan_slug
        )
        return {"status": "plan_restricted"}

    try:
        payload = InstagramWebhookPayload.model_validate_json(raw_body)
    except Exception as exc:
        logger.error("Failed to parse Instagram webhook: %s", exc)
        return {"status": "parse_error"}

    ig_user_id = payload.get_ig_user_id()

    dm = payload.get_first_dm()
    if dm and dm.message is not None:
        return await _handle_dm(db, ig_user_id, dm)

    comment = payload.get_first_comment()
    if comment:
        return await _handle_comment(db, ig_user_id, comment)

    return {"status": "ok"}


async def _handle_dm(
    db: AsyncSession,
    ig_user_id: str,
    dm: InstagramMessaging,
) -> dict:
    """
    Process an incoming Instagram DM (text, image, or audio) through the
    same channel-neutral order pipeline WhatsApp uses.

    Builds a WhatsAppMessage-shaped InboundContext (text/image/audio are all
    expressed via the same schema WhatsApp's webhook router already builds —
    see app/schemas/webhook.py) so handle_inbound_message() needs no IG-aware
    branching at all: slot machine, abuse protection, the address-confirm
    gate, vision-based product matching, and voice transcription all apply
    identically to Instagram.

    Args:
        db:         DB session.
        ig_user_id: Instagram Business Account ID.
        dm:         The parsed InstagramMessaging event.

    Returns:
        {"status": result.status} on success — {"status": "ok"} on any
        early DB/dedup failure, to prevent Meta from retrying.
    """
    sender_igsid = dm.get_sender_id()
    msg_type = dm.get_message_type()
    mid = dm.message.mid if dm.message else None

    client = await _get_active_client(db, ig_user_id)

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_igsid, channel="instagram", client_id=client.id if client else None
        )
    except Exception as exc:
        logger.error("DB error creating conversation for Instagram %s: %s", sender_igsid, exc)
        return {"status": "ok"}

    # Meta retries webhooks on timeout — skip if already processed (mirrors
    # webhook.py's wamid dedup; IG's mid serves the same purpose).
    if mid:
        dup_result = await db.execute(
            select(MessageModel).where(MessageModel.wamid == mid).limit(1)
        )
        if dup_result.scalar_one_or_none() is not None:
            logger.info("Duplicate Instagram message %s — skipping.", mid)
            return {"status": "ok"}

    if msg_type == "text":
        message = WhatsAppMessage(
            id=mid or "", **{"from": sender_igsid}, timestamp="0", type="text",
            text=TextContent(body=dm.get_text() or ""),
        )
        user_text = dm.get_text() or ""
    elif msg_type == "image":
        image_url = dm.message.get_image_url() if dm.message else None
        message = WhatsAppMessage(
            id=mid or "", **{"from": sender_igsid}, timestamp="0", type="image",
            image=ImageContent(id=image_url or ""),
        )
        user_text = "[image]"
    elif msg_type == "audio":
        # Ack send stays inline (not via PipelineResult) so the customer isn't
        # left waiting in silence during transcription — same exception
        # webhook.py documents for WhatsApp's audio ack.
        try:
            await instagram_service.send_dm(ig_user_id, sender_igsid, "🎤 Voice note suna. Ek second...")
        except Exception as exc:
            logger.warning("Ack DM failed for Instagram audio: %s", exc)
        audio_url = dm.message.get_audio_url() if dm.message else None
        message = WhatsAppMessage(
            id=mid or "", **{"from": sender_igsid}, timestamp="0", type="audio",
            audio=AudioContent(id=audio_url or "", mime_type="audio/mp4"),
        )
        user_text = "[voice note]"
    else:
        logger.info("Skipping unsupported Instagram message type '%s'.", msg_type)
        return {"status": "ok"}

    ctx = InboundContext(
        db=db,
        client=client,
        conv=conv,
        sender_phone=sender_igsid,
        message=message,
        user_text=user_text,
        wamid=mid,
        btn_nonce_parsed=None,
        download_media=vision_service.download_instagram_media,
        is_whatsapp=False,
    )
    result = await handle_inbound_message(ctx)

    try:
        await send_pipeline_result(result, ig_user_id=ig_user_id, recipient_igsid=sender_igsid)
    except Exception as exc:
        logger.error("Instagram send error: %s", exc)

    return {"status": result.status}


async def _handle_comment(
    db: AsyncSession,
    ig_user_id: str,
    comment,
) -> dict:
    """
    Process an Instagram comment through the comment → private-reply DM
    auto-trigger.

    Gating, in order (each is a "skip and log" no-op, not an error):
    - comment_id already processed (Meta webhook retry) → skip.
    - client.ig_comment_autoreply_enabled is False → skip (feature is opt-in).
    - not reply-all AND comment text matches none of the client's configured
      trigger keywords → skip.

    On match: an ig_comment_replies row is inserted (status="pending") before
    anything else — this is the durable record dedup checks against, so a
    retry arriving mid-processing already sees it and stops. If the client's
    IG account is under Meta's 200/hour automated-DM cap, the reply is sent
    immediately via _send_comment_reply(); otherwise the row stays "pending"
    and the scheduler's drain job (app/scheduler.py) sends it later.

    Args:
        db:         DB session.
        ig_user_id: Instagram Business Account ID.
        comment:    CommentChange object with field and value.

    Returns:
        {"status": "ok"} in all non-error cases (including every skip above),
        matching this router's "always 200 to Meta" contract.
    """
    commenter_igsid = comment.value.from_.id
    comment_text = comment.value.text
    comment_id = comment.value.id
    media_id = (comment.value.media or {}).get("id") if comment.value.media else None

    logger.info("Comment from %s: %s", commenter_igsid, comment_text)

    client = await _get_active_client(db, ig_user_id)
    if client is None:
        logger.warning("No active client for comment %s — skipping.", comment_id)
        return {"status": "ok"}

    dup_result = await db.execute(
        select(IgCommentReply).where(IgCommentReply.comment_id == comment_id).limit(1)
    )
    if dup_result.scalar_one_or_none() is not None:
        logger.info("Duplicate Instagram comment %s — skipping.", comment_id)
        return {"status": "ok"}

    if not client.ig_comment_autoreply_enabled:
        logger.info(
            "Comment auto-reply disabled for client=%s — skipping comment %s.",
            client.id, comment_id,
        )
        return {"status": "ok"}

    if not client.ig_comment_reply_all and not ig_comment_service.matches_trigger(
        comment_text, client.ig_comment_triggers
    ):
        logger.info(
            "Comment %s matched no trigger keyword for client=%s — skipping.",
            comment_id, client.id,
        )
        return {"status": "ok"}

    log_row = IgCommentReply(
        client_id=client.id,
        comment_id=comment_id,
        commenter_igsid=commenter_igsid,
        media_id=media_id,
        comment_text=comment_text,
        status="pending",
    )
    db.add(log_row)
    await db.commit()
    await db.refresh(log_row)

    if await ig_comment_service.is_comment_dm_rate_limited(ig_user_id):
        logger.warning(
            "event=comment_reply_rate_limited client=%s comment=%s — queued for drain job.",
            client.id, comment_id,
        )
        return {"status": "ok"}

    await ig_comment_service.send_comment_reply(db, client, ig_user_id, log_row)
    return {"status": "ok"}


class CommentReplyStatsOut(BaseModel):
    """Dashboard stats for the IG comment auto-reply feature."""

    today_sent: int
    total_comment_conversations: int
    converted_conversations: int
    conversion_rate: float


@router.get("/comment-stats", response_model=CommentReplyStatsOut)
async def get_comment_stats(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> CommentReplyStatsOut:
    """
    Return today's comment-reply count and comment→DM→order conversion for
    the dashboard's Comment Auto-Reply settings section.
    """
    stats = await ig_comment_service.get_comment_reply_stats(db, current_client.id)
    return CommentReplyStatsOut(**stats)
