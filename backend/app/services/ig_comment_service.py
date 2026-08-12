"""
IG comment → private-reply DM auto-trigger — shared logic.

Used by both app/routers/instagram.py (the inline path, for comments
processed the moment they arrive) and app/scheduler.py (the drain job, for
rows queued past Meta's 200/hour automated-DM cap). Kept as a service module
rather than router-private helpers so neither consumer has to import from
the other.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.ig_comment_reply import IgCommentReply
from app.models.order import Order
from app.schemas.webhook import TextContent, WhatsAppMessage
from app.services import conversation_service, outbound, vision_service
from app.services.language_service import detect_language
from app.services.order_pipeline import InboundContext, handle_inbound_message

logger = logging.getLogger(__name__)

# Meta's automated-DM cap for comment private replies: 200/hour per IG account.
# Same asyncio.Lock + defaultdict sliding-window shape as webhook.py's
# per-phone rate limiter, keyed by ig_user_id instead of phone.
_COMMENT_RATE_LIMIT_MESSAGES = 200
_COMMENT_RATE_LIMIT_WINDOW = 3600  # seconds
_comment_rate_limit_store: dict[str, list[datetime]] = defaultdict(list)
_comment_rate_lock = asyncio.Lock()

# detect_language() returns 6 codes; the client's ig_comment_reply_text is
# stored under 3 buckets — same collapse as language_templates.TEMPLATES.
_LANG_BUCKET = {
    "english": "english",
    "hindi_roman": "hindi",
    "hindi_devanagari": "hindi",
    "hinglish": "hindi",
    "gujarati_roman": "gujarati",
    "gujarati_script": "gujarati",
}


async def is_comment_dm_rate_limited(ig_user_id: str) -> bool:
    """Return True if ig_user_id has hit Meta's 200 automated-DMs/hour cap."""
    async with _comment_rate_lock:
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=_COMMENT_RATE_LIMIT_WINDOW)
        _comment_rate_limit_store[ig_user_id] = [
            ts for ts in _comment_rate_limit_store[ig_user_id] if ts > window_start
        ]
        if len(_comment_rate_limit_store[ig_user_id]) >= _COMMENT_RATE_LIMIT_MESSAGES:
            return True
        _comment_rate_limit_store[ig_user_id].append(now)
        return False


def matches_trigger(text: str, triggers: list[str] | None) -> bool:
    """Case-insensitive substring match of any trigger keyword/phrase in text."""
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in (triggers or []))


async def send_comment_reply(
    db: AsyncSession,
    client: Client,
    ig_user_id: str,
    log_row: IgCommentReply,
) -> None:
    """
    Perform the actual comment-reply send: public ack + pipeline-generated
    private-reply DM. Shared by the inline webhook path and the scheduler's
    drain job for previously-queued rows.

    Always updates log_row.status to "sent" or "failed" and commits — never
    raises, matching the rest of the Instagram send paths' best-effort style.
    """
    from app.routers._instagram_adapter import _numbered_text_fallback

    commenter_igsid = log_row.commenter_igsid
    comment_id = log_row.comment_id
    comment_text = log_row.comment_text

    # Public reply — client's configured text, language matched to the comment.
    try:
        lang_code = detect_language(comment_text)
        bucket = _LANG_BUCKET.get(lang_code, "english")
        reply_text_map = client.ig_comment_reply_text or {}
        public_text = (
            reply_text_map.get(bucket)
            or reply_text_map.get("english")
            or "Check your DM 👀"
        )
        await outbound.ig_reply_to_comment(ig_user_id, comment_id, public_text)
    except Exception as exc:
        logger.error("Public comment reply failed for comment=%s: %s", comment_id, exc)

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, commenter_igsid, channel="instagram",
            client_id=client.id, source="comment_reply",
        )
        message = WhatsAppMessage(
            id=comment_id, **{"from": commenter_igsid}, timestamp="0", type="text",
            text=TextContent(body=comment_text),
        )
        ctx = InboundContext(
            db=db,
            client=client,
            conv=conv,
            sender_phone=commenter_igsid,
            message=message,
            user_text=comment_text,
            wamid=comment_id,
            btn_nonce_parsed=None,
            download_media=vision_service.download_instagram_media,
            is_whatsapp=False,
        )
        result = await handle_inbound_message(ctx)

        # First-turn send is plain-text only — Private Reply's support for
        # interactive quick-reply payloads is unverified, so any buttons or
        # list options collapse to numbered text (same fallback the regular
        # rich adapter uses for choice sets IG quick replies can't represent).
        reply_text = result.text or ""
        if result.buttons:
            reply_text = _numbered_text_fallback(reply_text, result.buttons)
        elif result.list_options:
            reply_text = _numbered_text_fallback(reply_text, result.list_options)

        await outbound.ig_send_private_reply(
            ig_user_id, comment_id, reply_text,
            db=db, client_id=client.id, recipient_igsid=commenter_igsid,
        )

        log_row.status = "sent"
        log_row.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        logger.error("Private reply send failed for comment=%s: %s", comment_id, exc)
        log_row.status = "failed"

    await db.commit()


async def get_comment_reply_stats(db: AsyncSession, client_id: int) -> dict:
    """
    Return today's sent count and comment→DM→order conversion for a client.

    Conversion is computed over all-time (not just today) so it reads as a
    stable proof-of-value metric rather than a noisy daily rate: distinct
    source="comment_reply" conversations with >=1 Order, out of all such
    conversations.

    Returns:
        Dict with keys: today_sent, total_comment_conversations,
        converted_conversations, conversion_rate (0-1 float).
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    today_sent_result = await db.execute(
        select(func.count()).where(
            IgCommentReply.client_id == client_id,
            IgCommentReply.status == "sent",
            IgCommentReply.sent_at >= today_start,
        )
    )
    today_sent = today_sent_result.scalar_one() or 0

    total_result = await db.execute(
        select(func.count()).where(
            Conversation.client_id == client_id,
            Conversation.source == "comment_reply",
        )
    )
    total_comment_conversations = total_result.scalar_one() or 0

    converted_result = await db.execute(
        select(func.count(func.distinct(Conversation.id)))
        .select_from(Conversation)
        .join(Order, Order.conversation_id == Conversation.id)
        .where(
            Conversation.client_id == client_id,
            Conversation.source == "comment_reply",
        )
    )
    converted_conversations = converted_result.scalar_one() or 0

    conversion_rate = (
        converted_conversations / total_comment_conversations
        if total_comment_conversations
        else 0.0
    )

    return {
        "today_sent": today_sent,
        "total_comment_conversations": total_comment_conversations,
        "converted_conversations": converted_conversations,
        "conversion_rate": conversion_rate,
    }
