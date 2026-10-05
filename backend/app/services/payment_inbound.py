"""
Inbound-side hooks around order_pipeline.handle_inbound_message().

pre_process():  (1) auto-resume an idled-out human takeover, (2) download +
                re-host inbound image/audio in OUR storage, (3) manual-UPI
                proof handling — a screenshot while an order awaits payment
                becomes a PaymentProof (NO vision model), a bare "paid" with
                no image gets a deterministic "send the screenshot" reply.
post_process(): guarantee every inbound message is persisted with channel and
                media_url, and push realtime `new_message` events.

Both are best-effort wrappers: a failure here is logged and must never stop
the normal conversation flow from answering the customer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.models.message import Message
from app.services import (
    channel_sender,
    conversation_control,
    conversation_service,
    media_service,
    payment_verification_service as pvs,
)
from app.services.language_templates import get_template
from app.services.order_state_machine import ORDER_PAYMENT_SUBMITTED, ORDER_PENDING_PAYMENT

logger = logging.getLogger(__name__)


async def _max_message_id(db, conversation_id: int) -> int:
    """Highest message id in the conversation (0 when empty)."""
    return int(
        (await db.execute(
            select(func.coalesce(func.max(Message.id), 0)).where(Message.conversation_id == conversation_id)
        )).scalar_one()
    )


async def _ingest_media(ctx) -> None:
    """
    Download an inbound image/audio once, re-host it, and cache the bytes.

    Wraps ctx.download_media so the pipeline's own later download (OCR,
    transcription) reuses the cached bytes instead of hitting Meta twice.
    """
    msg = ctx.message
    media_id, media_type = None, None
    if msg.type == "image" and getattr(msg, "image", None) is not None:
        media_id, media_type = msg.image.id, "image"
    elif msg.type == "audio" and getattr(msg, "audio", None) is not None:
        media_id, media_type = msg.audio.id, "audio"
    if not media_id or getattr(ctx.conv, "is_sandbox", False):
        return

    original = ctx.download_media
    cache: dict[str, bytes] = {}

    async def _cached(mid):
        """Return cached bytes for mid, downloading on first use."""
        if mid not in cache:
            cache[mid] = await original(mid)
        return cache[mid]

    ctx.download_media = _cached
    try:
        data = await _cached(media_id)
        content_type = media_service.sniff_content_type(
            data, "image/jpeg" if media_type == "image" else "audio/ogg"
        )
        ctx.media_url = await media_service.store_media(ctx.client.id, data, content_type)
        ctx.media_type = media_type
    except Exception as exc:
        logger.error("Inbound media ingest failed conv=%s media_id=%s: %s", ctx.conv.id, media_id, exc)


async def _save_inbound(ctx, content: str) -> Message:
    """Persist the customer's message for the payment short-circuit paths."""
    return await conversation_service.save_message(
        ctx.db, ctx.conv.id, "user", content,
        original_type="image" if ctx.message.type == "image" else None,
        wamid=ctx.wamid, channel=ctx.conv.channel,
        media_url=ctx.media_url, media_type=ctx.media_type,
    )


async def _reply(ctx, key: str, *, stamp_ack: bool = False):
    """
    Deterministic template reply for a payment short-circuit.

    Silent (skip_send) while the bot is paused: the seller is handling the
    chat, but the proof/state change above has already been recorded.
    """
    from app.services.order_pipeline import PipelineResult

    conv = ctx.conv
    if conv.ai_enabled is False:
        return PipelineResult(text=None, skip_send=True)
    text = get_template(pvs._lang(conv), key)
    await conversation_service.save_message(
        ctx.db, conv.id, "assistant", text, channel=conv.channel, sender_type="bot",
    )
    if stamp_ack:
        conv.last_proof_ack_at = datetime.now(timezone.utc)
        await ctx.db.commit()
    return PipelineResult(text=text)


async def _handle_payment_message(ctx):
    """Proof / paid-word handling for a conversation with an order awaiting payment."""
    msg_type = ctx.message.type
    is_image = msg_type == "image"
    is_paid_text = msg_type in ("text", "interactive") and pvs.is_paid_intent(ctx.user_text)
    if not (is_image or is_paid_text):
        return None

    order = await pvs.find_open_payment_order(ctx.db, ctx.conv.id)
    if order is None:
        return None  # image/"paid" with no pending order → normal flow

    saved = await _save_inbound(ctx, ctx.user_text)

    if is_image:
        if not ctx.media_url:
            # Couldn't fetch the image from Meta — don't fake a proof.
            return await _reply(ctx, "pay_ask_screenshot")
        submission = await pvs.submit_proof(
            ctx.db, ctx.client, ctx.conv, order.id, message_id=saved.id, media_url=ctx.media_url,
        )
        if submission.transitioned:
            return await _reply(ctx, "pay_proof_received", stamp_ack=True)
        if pvs.proof_ack_due(ctx.conv):
            return await _reply(ctx, "pay_proof_more", stamp_ack=True)
        from app.services.order_pipeline import PipelineResult
        return PipelineResult(text=None, skip_send=True)

    # "paid" typed/tapped with no image
    if order.status == ORDER_PENDING_PAYMENT:
        return await _reply(ctx, "pay_ask_screenshot")
    if order.status == ORDER_PAYMENT_SUBMITTED and pvs.proof_ack_due(ctx.conv):
        return await _reply(ctx, "pay_proof_more", stamp_ack=True)
    from app.services.order_pipeline import PipelineResult
    return PipelineResult(text=None, skip_send=True)


async def pre_process(ctx):
    """Run the inbound pre-hooks; returns a PipelineResult to short-circuit, else None."""
    conv, client = ctx.conv, ctx.client
    if client is None or conv is None:
        return None
    try:
        await conversation_control.maybe_auto_resume(ctx.db, client, conv)
        await _ingest_media(ctx)
        return await _handle_payment_message(ctx)
    except Exception as exc:
        logger.error("payment_inbound.pre_process failed conv=%s: %s", getattr(conv, "id", None), exc)
        try:
            await ctx.db.rollback()
        except Exception:
            pass
        return None


async def post_process(ctx, before_max_id: int) -> None:
    """
    Make sure the turn's messages are fully persisted and announced.

    Guarantees an inbound row exists (early-exit guards such as the blocklist
    drop a message without saving it), back-fills channel and media_url, and
    publishes a `new_message` event per row created this turn.
    """
    conv, client, db = ctx.conv, ctx.client, ctx.db
    if client is None or conv is None:
        return
    try:
        inbound_this_turn = int(
            (await db.execute(
                select(func.count()).select_from(Message).where(
                    Message.conversation_id == conv.id, Message.id > before_max_id, Message.role == "user",
                )
            )).scalar_one()
        )
        if inbound_this_turn == 0:
            await _save_inbound(ctx, ctx.user_text)
        await db.execute(
            update(Message).where(Message.conversation_id == conv.id, Message.channel.is_(None))
            .values(channel=conv.channel)
        )
        if ctx.media_url and ctx.wamid:
            await db.execute(
                update(Message).where(
                    Message.conversation_id == conv.id, Message.wamid == ctx.wamid,
                    Message.media_url.is_(None),
                ).values(media_url=ctx.media_url, media_type=ctx.media_type)
            )
        await db.commit()

        rows = (
            await db.execute(
                select(Message).where(Message.conversation_id == conv.id, Message.id > before_max_id)
                .order_by(Message.id).execution_options(populate_existing=True)
            )
        ).scalars().all()
        for m in rows:
            from app.services import realtime_service
            realtime_service.publish(
                client.id, "new_message",
                {"conversation_id": conv.id, "message": channel_sender.message_to_dict(m)},
            )
    except Exception as exc:
        logger.error("payment_inbound.post_process failed conv=%s: %s", getattr(conv, "id", None), exc)
        try:
            await db.rollback()
        except Exception:
            pass
