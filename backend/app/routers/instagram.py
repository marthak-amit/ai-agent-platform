"""
Instagram webhook router.

Handles:
- GET  /instagram  : webhook verification (same Meta challenge flow as WhatsApp)
- POST /instagram  : incoming DMs and post comments

Message types handled:
- Text DMs: routed to Gemini for a conversational reply.
- Image DMs: image downloaded from the Instagram CDN URL (included directly
  in the payload, unlike WhatsApp which sends a media_id). vision_service
  matches the image against the client's catalogue.
- Story replies with images: same as image DMs — they arrive through the
  messaging[] array with type="image".
- Post comments: public reply (brief ack) + full AI reply via DM.

Comment flow: when a user comments on a post the agent posts a brief public
reply on the comment AND sends a full AI-generated reply via DM.
"""

import asyncio
import hashlib
import hmac
import logging
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.schemas.instagram import InstagramMessaging, InstagramWebhookPayload
from app.services import (
    catalogue_service,
    conversation_service,
    gemini_service,
    instagram_service,
    lead_service,
    plan_service,
    vision_service,
    voice_service,
)

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

    Used to gate Instagram processing before any payload parsing.
    Kept as a standalone async helper so tests can patch it independently.
    """
    client = await _get_active_client(db, ig_user_id)
    return (client.plan_slug if client else None) or "starter"


async def _get_catalogue_context(db: AsyncSession, client, user_text: str) -> str | None:
    """
    Build catalogue context for Gemini/vision from a customer message.

    SKU-first: if the message contains a product code, look up that exact
    product. Otherwise keyword-search the full catalogue and return the top 5.

    Args:
        db:        Active async DB session.
        client:    Active Client ORM instance, or None.
        user_text: The customer's message (used as search query).

    Returns:
        Formatted catalogue string, or None if no client or no relevant products.
    """
    if client is None:
        return None

    skus = catalogue_service.extract_skus_from_text(user_text)
    if skus:
        sku_products = []
        for sku in skus:
            p = await catalogue_service.find_product_by_sku(db, client.id, sku)
            if p:
                sku_products.append(p)
        if sku_products:
            return catalogue_service.format_catalogue_context(sku_products)

    products = await catalogue_service.list_products(db, client.id)
    relevant = catalogue_service.search_products(products, user_text)
    if not relevant:
        return None
    return catalogue_service.format_catalogue_context(relevant)


@router.post("", status_code=status.HTTP_200_OK)
async def receive_instagram_event(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle incoming Instagram DMs and post comments.

    Validates X-Hub-Signature-256 before processing anything.
    Requires growth or pro plan — starter plan returns plan_restricted.

    DM flow (text):  user sends DM → Gemini reply → send DM back.
    DM flow (image): download image from CDN URL → vision_service → send DM back.
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
    if not plan_service.plan_allows_channel(plan_slug, "instagram"):
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
    if dm:
        msg_type = dm.get_message_type()
        if msg_type == "image":
            return await _handle_image_dm(db, ig_user_id, dm)
        if msg_type == "audio":
            return await _handle_audio_dm(db, ig_user_id, dm)
        if dm.get_text():
            return await _handle_dm(db, ig_user_id, dm.get_sender_id(), dm.get_text())

    comment = payload.get_first_comment()
    if comment:
        return await _handle_comment(db, ig_user_id, comment)

    return {"status": "ok"}


async def _handle_dm(
    db: AsyncSession,
    ig_user_id: str,
    sender_igsid: str,
    user_text: str,
) -> dict:
    """
    Process an incoming Instagram text DM and reply with an AI response.

    Args:
        db:           DB session.
        ig_user_id:   Instagram Business Account ID.
        sender_igsid: Sender's IGSID.
        user_text:    Message text.

    Returns:
        {"status": "ok"} on success.
    """
    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_igsid, channel="instagram"
        )
    except Exception as exc:
        logger.error("DB error creating conversation for Instagram %s: %s", sender_igsid, exc)
        return {"status": "ok"}

    # Human takeover: save message silently, skip AI entirely.
    if conv.ai_enabled is False:
        await conversation_service.save_message(db, conv.id, "user", user_text)
        logger.info(
            "AI paused for Instagram %s — human takeover active, message saved silently.",
            sender_igsid,
        )
        return {"status": "ok"}

    settings = get_settings()
    delay = random.uniform(settings.min_reply_delay, settings.max_reply_delay)
    await asyncio.sleep(delay)

    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    try:
        ai_reply = await gemini_service.generate_reply(user_text, history=history_dicts)
    except Exception as exc:
        logger.error("Gemini error on Instagram DM: %s", exc)
        return {"status": "ok"}

    await conversation_service.save_message(db, conv.id, "user", user_text)
    await conversation_service.save_message(db, conv.id, "model", ai_reply)

    all_messages = history_dicts + [
        {"role": "user", "content": user_text},
        {"role": "model", "content": ai_reply},
    ]
    try:
        await lead_service.tag_lead(db, sender_igsid, conv.id, all_messages)
    except Exception as exc:
        logger.error("Lead tagging error on Instagram DM: %s", exc)

    try:
        await instagram_service.send_dm(ig_user_id, sender_igsid, ai_reply)
    except Exception as exc:
        logger.error("Instagram DM send error: %s", exc)

    return {"status": "ok"}


async def _handle_image_dm(
    db: AsyncSession,
    ig_user_id: str,
    dm: InstagramMessaging,
) -> dict:
    """
    Process an Instagram DM that contains an image attachment.

    Applies to:
    - Direct image DMs (customer sends a product photo asking for price/availability).
    - Story replies where the customer attaches an image.

    Flow:
    1. Download the image from the CDN URL embedded in the payload.
    2. Load the client's catalogue context.
    3. Run vision_service.analyze_product_image() to identify the product.
    4. Save messages and reply via DM.

    Args:
        db:         DB session.
        ig_user_id: Instagram Business Account ID.
        dm:         The parsed InstagramMessaging event (type == "image").

    Returns:
        {"status": "ok"} on success.
    """
    sender_igsid = dm.get_sender_id()
    image_url = dm.message.get_image_url() if dm.message else None

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_igsid, channel="instagram"
        )
    except Exception as exc:
        logger.error("DB error creating conversation for Instagram image DM %s: %s", sender_igsid, exc)
        return {"status": "ok"}

    if conv.ai_enabled is False:
        await conversation_service.save_message(db, conv.id, "user", "[image]")
        logger.info("AI paused for Instagram %s — image saved silently.", sender_igsid)
        return {"status": "ok"}

    if not image_url:
        logger.warning("Instagram image DM from %s has no URL in payload.", sender_igsid)
        await conversation_service.save_message(db, conv.id, "user", "[image]")
        fallback = "Image receive hua lekin process nahi ho paya. Please try again."
        await conversation_service.save_message(db, conv.id, "model", fallback)
        try:
            await instagram_service.send_dm(ig_user_id, sender_igsid, fallback)
        except Exception as exc:
            logger.error("Instagram DM send error (image fallback): %s", exc)
        return {"status": "ok"}

    logger.info("Instagram image DM from %s, url=%s", sender_igsid, image_url[:60])

    client = await _get_active_client(db, ig_user_id)
    catalogue_context = await _get_catalogue_context(db, client, "image")

    try:
        image_bytes = await vision_service.download_instagram_media(image_url)
        ai_reply = await vision_service.analyze_product_image(
            image_bytes, catalogue_context or ""
        )
    except Exception as exc:
        logger.error("Vision service error on Instagram image DM: %s", exc)
        ai_reply = "Aapki image receive ho gayi! Kuch technical issue ke wajah se process nahi ho paya. Kripya dobara try karein."

    await conversation_service.save_message(db, conv.id, "user", "[image]")
    await conversation_service.save_message(db, conv.id, "model", ai_reply)

    try:
        await lead_service.tag_lead(
            db,
            sender_igsid,
            conv.id,
            [{"role": "user", "content": "[image]"}, {"role": "model", "content": ai_reply}],
        )
    except Exception as exc:
        logger.error("Lead tagging error on Instagram image DM: %s", exc)

    try:
        await instagram_service.send_dm(ig_user_id, sender_igsid, ai_reply)
    except Exception as exc:
        logger.error("Instagram DM send error (image reply): %s", exc)

    return {"status": "ok"}


async def _handle_audio_dm(
    db: AsyncSession,
    ig_user_id: str,
    dm: InstagramMessaging,
) -> dict:
    """
    Process an Instagram DM that contains an audio/voice attachment.

    Flow:
    1. Send an acknowledgement DM so the customer isn't left waiting.
    2. Download the audio bytes from the CDN URL in the attachment payload.
    3. Transcribe with Groq Whisper.
    4. Generate an AI reply using the transcription as user text.
    5. Save messages (user with original_type='audio') and reply via DM.

    Args:
        db:         DB session.
        ig_user_id: Instagram Business Account ID.
        dm:         The parsed InstagramMessaging event (type == "audio").

    Returns:
        {"status": "ok"} on success.
    """
    sender_igsid = dm.get_sender_id()
    audio_url = dm.message.get_audio_url() if dm.message else None

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_igsid, channel="instagram"
        )
    except Exception as exc:
        logger.error("DB error creating conversation for Instagram audio DM %s: %s", sender_igsid, exc)
        return {"status": "ok"}

    if conv.ai_enabled is False:
        await conversation_service.save_message(db, conv.id, "user", "[voice note]", original_type="audio")
        logger.info("AI paused for Instagram %s — audio saved silently.", sender_igsid)
        return {"status": "ok"}

    # Acknowledge receipt before the slow transcription step
    try:
        await instagram_service.send_dm(ig_user_id, sender_igsid, "🎤 Voice note suna. Ek second...")
    except Exception as exc:
        logger.warning("Ack DM failed for Instagram audio: %s", exc)

    transcribed_text = ""
    if audio_url:
        try:
            import httpx
            settings = get_settings()
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(
                    audio_url,
                    headers={"Authorization": f"Bearer {settings.instagram_access_token}"},
                )
                resp.raise_for_status()
                audio_bytes = resp.content
            transcribed_text = await voice_service.transcribe_voice_note(audio_bytes, "audio.mp4")
        except Exception as exc:
            logger.error("Instagram audio download/transcription error: %s", exc)
    else:
        logger.warning("Instagram audio DM from %s has no URL.", sender_igsid)

    client = await _get_active_client(db, ig_user_id)
    effective_text = transcribed_text if transcribed_text else "[voice note]"
    catalogue_context = await _get_catalogue_context(db, client, effective_text)
    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    try:
        ai_reply = await gemini_service.generate_reply(effective_text, history=history_dicts)
    except Exception as exc:
        logger.error("Gemini error on Instagram audio DM: %s", exc)
        return {"status": "ok"}

    saved_user_content = transcribed_text if transcribed_text else "[voice note]"
    await conversation_service.save_message(db, conv.id, "user", saved_user_content, original_type="audio")
    if transcribed_text:
        ai_reply = f"_{transcribed_text}_\n\n{ai_reply}"
    await conversation_service.save_message(db, conv.id, "model", ai_reply)

    try:
        await lead_service.tag_lead(
            db,
            sender_igsid,
            conv.id,
            history_dicts + [
                {"role": "user", "content": saved_user_content},
                {"role": "model", "content": ai_reply},
            ],
        )
    except Exception as exc:
        logger.error("Lead tagging error on Instagram audio DM: %s", exc)

    try:
        await instagram_service.send_dm(ig_user_id, sender_igsid, ai_reply)
    except Exception as exc:
        logger.error("Instagram DM send error (audio reply): %s", exc)

    return {"status": "ok"}


async def _handle_comment(
    db: AsyncSession,
    ig_user_id: str,
    comment,
) -> dict:
    """
    Process an Instagram comment: post a public reply and send full details via DM.

    Two-step reply strategy:
    - Public reply (visible under the post): brief acknowledgement so the thread
      looks active to other viewers.
    - DM (private): full AI-generated response with product details, pricing, etc.

    Args:
        db:         DB session.
        ig_user_id: Instagram Business Account ID.
        comment:    CommentChange object with field and value.

    Returns:
        {"status": "ok"} on success.
    """
    commenter_igsid = comment.value.from_.id
    comment_text = comment.value.text
    comment_id = comment.value.id

    logger.info("Comment from %s: %s", commenter_igsid, comment_text)

    conv = await conversation_service.get_or_create_conversation(
        db, commenter_igsid, channel="instagram"
    )
    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    try:
        ai_reply = await gemini_service.generate_reply(comment_text, history=history_dicts)
    except Exception as exc:
        logger.error("Gemini error on comment: %s", exc)
        return {"status": "ok"}

    await conversation_service.save_message(db, conv.id, "user", comment_text)
    await conversation_service.save_message(db, conv.id, "model", ai_reply)

    try:
        await lead_service.tag_lead(
            db,
            commenter_igsid,
            conv.id,
            history_dicts + [
                {"role": "user", "content": comment_text},
                {"role": "model", "content": ai_reply},
            ],
        )
    except Exception as exc:
        logger.error("Lead tagging error on comment: %s", exc)

    # Public reply on the comment thread — brief so the post stays clean.
    # The full AI response goes to DM below.
    try:
        public_ack = "Thanks for your comment! Sending you the details in DM. 😊"
        await instagram_service.reply_to_comment(ig_user_id, comment_id, public_ack)
    except Exception as exc:
        logger.error("Instagram public comment reply failed: %s", exc)

    # Full AI reply via DM — not length-limited, contains pricing/product details.
    try:
        await instagram_service.send_dm(ig_user_id, commenter_igsid, ai_reply)
    except Exception as exc:
        logger.error("Instagram DM reply to commenter failed: %s", exc)

    return {"status": "ok"}
