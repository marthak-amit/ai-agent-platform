"""
Inbound-side hooks around order_pipeline.handle_inbound_message().

pre_process():  (1) auto-resume an idled-out human takeover, (2) download +
                re-host inbound image/audio in OUR storage, (3) manual-UPI
                proof handling — an image sent while an order awaits payment is CLASSIFIED first
                (payment_proof_classifier: caption → catalogue watermark → OCR → vision only when
                unsure). A payment proof is filed under the right order (amount match; the customer
                is asked when several fit), a product photo goes to the normal product flow, an
                unsure image is asked about with buttons. "This is not the payment" withdraws the
                last proof. A bare "paid" with no image gets a deterministic "send the screenshot" reply.
post_process(): guarantee every inbound message is persisted with channel and
                media_url, and push realtime `new_message` events.

Both are best-effort wrappers: a failure here is logged and must never stop
the normal conversation flow from answering the customer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select, update

from app.config import get_settings
from app.models.message import Message
from app.models.order import Order
from app.models.payment_proof import PaymentProof
from app.services import (
    catalogue_snapshot,
    channel_sender,
    conversation_control,
    conversation_service,
    image_fingerprint,
    media_service,
    payment_amounts,
    payment_proof_classifier,
    payment_verification_service as pvs,
    turn_trace,
)
from app.services.language_templates import format_price, get_template
from app.services.order_state_machine import ORDER_PAID, ORDER_PAYMENT_SUBMITTED, ORDER_PENDING_PAYMENT

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
        return PipelineResult(text=None, skip_send=True, skip_reason="ai_paused")
    text = get_template(pvs._lang(conv), key)
    await conversation_service.save_message(
        ctx.db, conv.id, "assistant", text, channel=conv.channel, sender_type="bot",
    )
    if stamp_ack:
        conv.last_proof_ack_at = datetime.now(timezone.utc)
        await ctx.db.commit()
    return PipelineResult(text=text)


# Button ids of the questions this module asks (the pipeline's nonce suffix is stripped by the webhook: ``ctx.btn_nonce_parsed[0]``).
BTN_PROOF_PAYMENT = "v3_proof_payment"
BTN_PROOF_PRODUCT = "v3_proof_product"
BTN_PROOF_ORDER_PREFIX = "v3_proof_order:"
BTN_PROOF_CHANGE = "v3_proof_change"      # "Wrong order? [Change order]" after a screenshot was attached by recency
#: A tapped answer refers to the customer's images of the last hour.
_RECENT_IMAGE = timedelta(hours=1)
#: WhatsApp reply buttons: up to 3; beyond that the orders go into a list message (up to 10 rows).
_MAX_BUTTON_ORDERS = 3
_MAX_LIST_ORDERS = 10


def _aware(value: datetime | None) -> datetime | None:
    """A timezone-aware copy of a datetime (naive values are taken as UTC)."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    """Current UTC time (one place to patch)."""
    return datetime.now(timezone.utc)


def pending_only(orders: list) -> list:
    """The orders still waiting for the customer's money (``pending_payment``), newest first — the only ones a screenshot can be filed under."""
    return [o for o in orders if o.status == ORDER_PENDING_PAYMENT]


def prior_payment(ctx, orders: list) -> bool:
    """
    True when an order waits for money AND its payment instructions went out within ``payment_proof_prior_minutes``: an unclear image
    sent then is far more likely a screenshot than a product photo.
    """
    pending = pending_only(orders)
    if not pending:
        return False
    sent = _aware(getattr(ctx.conv, "last_payment_instruction_at", None))
    if sent is None:
        stamps = [_aware(getattr(o, "created_at", None)) for o in pending]
        sent = max((t for t in stamps if t is not None), default=None)
    if sent is None:
        return False
    return _now() - sent <= timedelta(minutes=get_settings().payment_proof_prior_minutes)


def _in_image_burst(conv) -> bool:
    """True when a reply to an image went out less than ``payment_image_batch_seconds`` ago (a burst of images gets ONE reply)."""
    last = _aware(getattr(conv, "image_batch_reply_at", None))
    return last is not None and (_now() - last).total_seconds() < get_settings().payment_image_batch_seconds


async def image_burst(ctx) -> bool:
    """
    Is this image part of a burst that already has its reply? True when a reply to an image went out under a minute ago AND the customer's
    previous message was an image too (anything typed in between starts a new exchange: its images are answered).
    """
    if not _in_image_burst(ctx.conv):
        return False
    last_media = (await ctx.db.execute(
        select(Message.media_type).where(Message.conversation_id == ctx.conv.id, Message.role == "user").order_by(Message.id.desc()).limit(1)
    )).scalar_one_or_none()
    return last_media == "image"


async def _claim_image_reply(ctx) -> bool:
    """
    May this image turn send a reply? False when the previous image's reply is under a minute old (the burst already has its answer);
    otherwise the reply slot is stamped and True returned.
    """
    if getattr(ctx.message, "type", None) != "image":
        return True          # a typed answer / button tap ("Payment screenshot") is the customer speaking: always answered, never a burst image
    if await image_burst(ctx):
        logger.info("Image burst conv=%s — no reply of its own for this image", ctx.conv.id)
        return False
    ctx.conv.image_batch_reply_at = _now()
    await ctx.db.commit()
    return True


def _silent(reason: str = "image_burst"):
    """A deliberately empty result (the turn is recorded, nothing is sent). `reason` is checked by ``order_pipeline.ensure_reply``."""
    from app.services.order_pipeline import PipelineResult

    return PipelineResult(text=None, skip_send=True, skip_reason=reason)


async def _reply_with_buttons(ctx, text: str, buttons: list[tuple[str, str]]):
    """A deterministic reply with up to 3 buttons (the WhatsApp adapter adds the nonce); silent while a human has the chat."""
    from app.services.order_pipeline import ButtonSpec, PipelineResult

    conv = ctx.conv
    if conv.ai_enabled is False:
        return PipelineResult(text=None, skip_send=True, skip_reason="ai_paused")
    await conversation_service.save_message(ctx.db, conv.id, "assistant", text, channel=conv.channel, sender_type="bot")
    return PipelineResult(text=text, buttons=[ButtonSpec(i, t) for i, t in buttons] if ctx.is_whatsapp else None)


async def _reply_text(ctx, text: str):
    """A deterministic plain-text reply (already rendered); silent while a human has the chat."""
    from app.services.order_pipeline import PipelineResult

    conv = ctx.conv
    if conv.ai_enabled is False:
        return PipelineResult(text=None, skip_send=True, skip_reason="ai_paused")
    await conversation_service.save_message(ctx.db, conv.id, "assistant", text, channel=conv.channel, sender_type="bot")
    return PipelineResult(text=text)


async def _watermark_sku(ctx, image_bytes: bytes | None) -> str | None:
    """The product code of THIS shop's catalogue read from the image's watermark (our own product photos carry one), else None."""
    if not image_bytes or getattr(ctx, "client", None) is None:
        return None
    try:
        from app.services import catalogue_service, ocr_service

        code = await asyncio.to_thread(ocr_service.detect_sku_from_image, image_bytes)
        if code:
            product = await catalogue_service.find_product_by_sku(ctx.db, ctx.client.id, code)
            if product is not None and (product.sku or "").upper() == code.upper():
                return code
    except Exception as exc:  # noqa: BLE001
        logger.error("Proof classification: watermark read failed conv=%s: %s", getattr(ctx.conv, "id", None), exc)
    return None


async def _catalogue_match(ctx, image_bytes: bytes | None) -> str | None:
    """The catalogue product this picture is a copy of (perceptual hash; cached index only, hashed off the event loop; the index itself is built in the background by ``image_fingerprint.ensure_index``)."""
    if not image_bytes or getattr(ctx, "client", None) is None:
        return None
    try:
        rows = catalogue_snapshot.get_stale(ctx.client.id) or ()
        image_fingerprint.ensure_index(ctx.client.id, [(r.sku, r.image_url) for r in rows if r.sku and r.image_url])
        return await image_fingerprint.match_async(ctx.client.id, image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalogue image match failed conv=%s: %s", getattr(ctx.conv, "id", None), exc)
        return None


def _upi_ids(ctx) -> list[str]:
    """The shop's UPI id(s): seeing one on a picture is a strong payment signal."""
    upi = getattr(getattr(ctx, "client", None), "upi_id", None)
    return [upi] if isinstance(upi, str) and "@" in upi else []


async def classify_inbound_image(ctx, orders: list) -> "payment_proof_classifier.ProofVerdict":
    """
    What is this image? (caption → catalogue watermark / hash → OCR and the vision model side by side — see ``payment_proof_classifier``.)

    Logged as one ``PROOF_CLASSIFY`` line: the verdict and everything it rests on (OCR length, vision label, the recent-instructions prior,
    which evidence decided) — the evidence behind every attach / ask / reroute.
    """
    caption = getattr(getattr(ctx.message, "image", None), "caption", None)
    image_bytes = None
    try:
        image_bytes = await ctx.download_media(ctx.message.image.id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Proof classification: image download failed conv=%s: %s", ctx.conv.id, exc)
    prior = prior_payment(ctx, orders)
    targets = pending_only(orders) or orders
    from app.db import release_connection

    await release_connection(ctx.db)          # OCR + the vision model take seconds: do not hold a pooled connection while they run
    verdict = await payment_proof_classifier.classify(
        image_bytes, order_totals=[float(o.total_amount or 0) for o in targets], caption=caption,
        sku_probe=(lambda: _watermark_sku(ctx, image_bytes)) if image_bytes else None, catalogue_match=await _catalogue_match(ctx, image_bytes),
        client_id=getattr(ctx.client, "id", None), conversation_id=ctx.conv.id, prior_payment=prior, upi_ids=_upi_ids(ctx),
    )
    ctx.image_bytes = image_bytes
    logger.info(
        "PROOF_CLASSIFY conv=%s kind=%s conf=%.2f decided_by=%s ocr_text_len=%s vision_label=%s vision_conf=%s prior=%s signals=%s amounts=%s orders=%s",
        ctx.conv.id, verdict.kind, verdict.confidence, verdict.decided_by, verdict.ocr_len, verdict.vision_label,
        None if verdict.vision_conf is None else round(verdict.vision_conf, 2), str(verdict.prior).lower(), verdict.signals,
        verdict.amounts[:4], [o.order_number for o in orders],
    )
    return verdict


def pick_order_for_amounts(orders: list, amounts: list[float]) -> tuple[object | None, bool]:
    """
    Which unpaid order does a proof with these amounts belong to? ``(order, ambiguous)``.

    One open order → that one. Several → the single one whose total appears on the receipt (±₹1); none or more than one match is ambiguous
    (the customer is asked) — a screenshot is never filed under a guess.
    """
    if len(orders) == 1:
        return orders[0], False
    hits = [o for o in orders if payment_proof_classifier.matches_total(amounts, float(o.total_amount or 0))]
    if len(hits) == 1:
        return hits[0], False
    return None, True


def pick_order_by_recency(orders: list, *, last_instruction_at: datetime | None = None, now: datetime | None = None):
    """
    No amount to go by: attach to the NEWEST unpaid order when it is plainly the one being paid — its payment instructions went out at most
    ``picker_auto_attach_recent_minutes`` ago AND every other unpaid order's instructions are older than ``picker_auto_attach_others_older_hours``.
    Otherwise None (the customer is asked). An order's instructions went out when it was created (the newest one's stamp may be later: a re-sent
    instruction after an edit — ``last_instruction_at``, the conversation's last one).
    """
    candidates = pending_only(orders)
    if len(candidates) < 2:
        return None
    settings = get_settings()
    now = now or _now()
    ranked = sorted(candidates, key=lambda o: _aware(getattr(o, "created_at", None)) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    newest, others = ranked[0], ranked[1:]
    sent = _aware(getattr(newest, "created_at", None))
    stamp = _aware(last_instruction_at)
    if stamp is not None and (sent is None or stamp > sent):
        sent = stamp
    if sent is None or now - sent > timedelta(minutes=settings.picker_auto_attach_recent_minutes):
        return None
    cutoff = timedelta(hours=settings.picker_auto_attach_others_older_hours)
    for other in others:
        created = _aware(getattr(other, "created_at", None))
        if created is None or now - created <= cutoff:
            return None
    return newest


def resolve_target(ctx, targets: list, amounts: list[float]) -> tuple[object | None, bool, bool]:
    """``(order, ambiguous, chosen_by_recency)``: the amount picks the order; failing that the recency rule may; otherwise the customer is asked."""
    order, ambiguous = pick_order_for_amounts(targets, amounts)
    if not ambiguous:
        return order, False, False
    recent = pick_order_by_recency(targets, last_instruction_at=getattr(ctx.conv, "last_payment_instruction_at", None))
    return (recent, False, True) if recent is not None else (None, True, False)


async def _ask_which_order(ctx, orders: list):
    """
    "Which order is this payment for?" — only orders still waiting for money (newest first). Up to 3 → one button each; more → a
    list message (WhatsApp) / numbered text.
    """
    from app.services.order_pipeline import ListOptionSpec, PipelineResult

    if not await _claim_image_reply(ctx):
        return _silent()
    lang = pvs._lang(ctx.conv)
    candidates = pending_only(orders) or orders
    shown = candidates[:_MAX_LIST_ORDERS]
    lines = "\n".join(f"• {o.order_number} — {format_price(o.total_amount)}" for o in shown)
    text = f"{get_template(lang, 'pay_which_order')}\n{lines}"
    if len(shown) <= _MAX_BUTTON_ORDERS:
        return await _reply_with_buttons(ctx, text, [(f"{BTN_PROOF_ORDER_PREFIX}{o.id}", o.order_number) for o in shown])
    if ctx.conv.ai_enabled is False:
        return _silent("ai_paused")
    await conversation_service.save_message(ctx.db, ctx.conv.id, "assistant", text, channel=ctx.conv.channel, sender_type="bot")
    if not ctx.is_whatsapp:
        return PipelineResult(text=text)
    return PipelineResult(
        text=get_template(lang, "pay_which_order"),
        list_options=[ListOptionSpec(f"{BTN_PROOF_ORDER_PREFIX}{o.id}", o.order_number, format_price(o.total_amount)) for o in shown],
        list_header=get_template(lang, "pay_which_order")[:60], list_button_text=get_template(lang, "pay_pick_order_button"),
    )


async def _ask_image_kind(ctx, first_image_id: int | None = None):
    """
    "Is this your payment screenshot or a product photo?" — asked when the evidence is not enough to decide. The first image of the burst
    is remembered on the conversation (``pending_proof_image_id``) so the answer applies to all of them; further images get no question.
    """
    pending = getattr(ctx.conv, "pending_proof_image_id", None)
    if first_image_id is not None and not pending:
        ctx.conv.pending_proof_image_id = first_image_id
        await ctx.db.commit()
    if not await _claim_image_reply(ctx):
        return _silent()
    lang = pvs._lang(ctx.conv)
    return await _reply_with_buttons(
        ctx, get_template(lang, "pay_ask_image_kind"),
        [(BTN_PROOF_PAYMENT, get_template(lang, "btn_proof_payment")), (BTN_PROOF_PRODUCT, get_template(lang, "btn_proof_product"))],
    )


async def _attach_images(ctx, order, images: list[Message], *, multiple_orders: bool, reclassified: bool = False, auto: bool = False):
    """
    File the given saved images under `order` as payment proofs and answer once (naming the order when the customer has several).
    `auto` = the order was chosen by recency, not by the customer or the amount: the reply says so and offers [Change order].
    """
    submission = None
    for image in images:
        extra = {"reclassified": True} if reclassified else {}
        got = await pvs.submit_proof(
            ctx.db, ctx.client, ctx.conv, order.id, message_id=image.id, media_url=getattr(image, "media_url", None) or ctx.media_url, **extra,
        )
        submission = submission or got
        if got.transitioned:
            submission = got
    if getattr(ctx.conv, "pending_proof_image_id", None):
        ctx.conv.pending_proof_image_id = None
        await ctx.db.commit()
    if submission is not None and submission.transitioned:
        if auto and not submission.high_value:
            lang = pvs._lang(ctx.conv)
            text = get_template(lang, "pay_auto_attached", order_number=order.order_number, amount=format_price(order.total_amount))
            logger.info("PROOF_AUTO_ATTACHED conv=%s order=%s (no readable amount; newest unpaid order, recent instructions)", ctx.conv.id, order.order_number)
            return await _reply_with_buttons(ctx, text, [(BTN_PROOF_CHANGE, get_template(lang, "btn_proof_change"))])
        if multiple_orders and not submission.high_value:
            lang = pvs._lang(ctx.conv)
            return await _reply_text(ctx, get_template(lang, "pay_proof_received_order", order_number=order.order_number))
        return await _reply(ctx, "pay_proof_received_high_value" if submission.high_value else "pay_proof_received", stamp_ack=True)
    if pvs.proof_ack_due(ctx.conv) or getattr(ctx.message, "type", None) != "image":
        return await _reply(ctx, "pay_proof_more", stamp_ack=True)      # a tap / typed answer always gets its reply
    return _silent("ack_throttled")


async def _attach_proof(ctx, order, message_id: int, *, multiple_orders: bool):
    """File one saved image under `order` as a payment proof (kept for callers that hold only a message id)."""
    image = SimpleNamespace(id=message_id, media_url=ctx.media_url)
    return await _attach_images(ctx, order, [image], multiple_orders=multiple_orders)


async def _unattached_images(ctx, *, since: timedelta, from_id: int | None = None, include_withdrawn: bool = False) -> list[Message]:
    """
    The customer's image messages of the last `since` that are not yet filed as a payment proof (oldest first). A WITHDRAWN proof still
    counts as "filed" (the customer said the image is not a payment) unless `include_withdrawn` — used when the customer re-routes it
    themselves ([Change order] → "which order is this payment for?").
    """
    cutoff = _now() - since
    query = select(Message).where(
        Message.conversation_id == ctx.conv.id, Message.role == "user", Message.media_type == "image",
        Message.media_url.isnot(None), Message.created_at >= cutoff,
    )
    if from_id:
        query = query.where(Message.id >= from_id)
    rows = (await ctx.db.execute(query.order_by(Message.id.asc()))).scalars().all()
    if not rows:
        return []
    taken_query = select(PaymentProof.message_id).where(PaymentProof.message_id.in_([m.id for m in rows]))
    if include_withdrawn:
        taken_query = taken_query.where(PaymentProof.status != "withdrawn")
    taken = (await ctx.db.execute(taken_query)).scalars().all()
    return [m for m in rows if m.id not in set(taken)]


async def _latest_unattached_image(ctx) -> Message | None:
    """The customer's newest image message of the last hour that is not yet filed as a payment proof."""
    images = await _unattached_images(ctx, since=_RECENT_IMAGE)
    return images[-1] if images else None


async def _answer_images(ctx) -> list[Message]:
    """The images a tapped answer refers to: everything since the first unanswered image of the burst, else just the latest one."""
    first = getattr(ctx.conv, "pending_proof_image_id", None)
    if first:
        images = await _unattached_images(ctx, since=_RECENT_IMAGE, from_id=first, include_withdrawn=True)
        if images:
            return images
    latest = await _latest_unattached_image(ctx)
    return [latest] if latest is not None else []


async def _amounts_of(images: list[Message], limit: int = 3) -> list[float]:
    """Amounts read (OCR) from the stored images — used to pick the order after the customer confirmed an image is the payment."""
    found: list[float] = []
    for image in images[-limit:]:
        data = await media_service.read_media(image.media_url)
        text = await payment_proof_classifier.ocr_text(data) if data else None
        found.extend(payment_amounts.extract_amounts(text or ""))
    return sorted(set(found), reverse=True)


async def _latest_image_link(ctx) -> tuple[int, str] | None:
    """(order id, order number) the customer's newest image was filed under as a proof, or None when it is not filed anywhere."""
    msg = (await ctx.db.execute(
        select(Message).where(Message.conversation_id == ctx.conv.id, Message.role == "user", Message.media_type == "image")
        .order_by(Message.id.desc()).limit(1)
    )).scalar_one_or_none()
    if msg is None:
        return None
    row = (await ctx.db.execute(
        select(PaymentProof.order_id, Order.order_number).join(Order, Order.id == PaymentProof.order_id)
        .where(PaymentProof.message_id == msg.id).order_by(PaymentProof.id.desc()).limit(1)
    )).first()
    return (int(row[0]), str(row[1])) if row else None


async def _handle_proof_tap(ctx, button_id: str):
    """The customer's answer to "payment screenshot or product photo?" / "which order?" (stale taps get an explanation, never a wrong filing)."""
    lang = pvs._lang(ctx.conv)
    if button_id == BTN_PROOF_CHANGE:
        return await _handle_change_order(ctx)
    if button_id == BTN_PROOF_PRODUCT:
        images = await _answer_images(ctx)
        ctx.conv.pending_proof_image_id = None
        await ctx.db.commit()
        if images:
            from app.services import agent_v3

            data = await media_service.read_media(images[-1].media_url) if agent_v3.enabled(ctx.client) else None
            if data:
                ctx.photo_bytes = data            # the product flow looks at THIS picture (similar products), not at the button label
                ctx.image_is_product_photo = True
                return None
        return await _reply_text(ctx, get_template(lang, "pay_product_photo_ask"))
    orders = await pvs.find_open_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
    if not orders:
        return None
    targets = pending_only(orders) or orders
    images = await _answer_images(ctx)
    if button_id.startswith(BTN_PROOF_ORDER_PREFIX):
        wanted_id = button_id[len(BTN_PROOF_ORDER_PREFIX):]
        if not images:
            return await _stale_pick_reply(ctx, wanted_id)
        order = next((o for o in targets if str(o.id) == wanted_id), None)
        if order is None:
            return await _stale_pick_reply(ctx, wanted_id, orders=orders)
        return await _attach_images(ctx, order, images, multiple_orders=len(targets) > 1)
    if not images:
        return await _reply(ctx, "pay_ask_screenshot")            # nothing to attach (already filed, or older than an hour): ask for the screenshot
    ctx.media_url = images[-1].media_url
    # "it IS a payment screenshot": the amount on it picks the order when it can; with several unpaid orders and no readable amount the customer says which
    order, ambiguous, auto = resolve_target(ctx, targets, await _amounts_of(images)) if len(targets) > 1 else (targets[0], False, False)
    if ambiguous:
        return await _ask_which_order(ctx, orders)
    return await _attach_images(ctx, order, images, multiple_orders=len(targets) > 1, auto=auto)


async def _handle_change_order(ctx):
    """
    "Wrong order? [Change order]" after a screenshot was attached by recency: the proof is withdrawn (audit kept, the order waits for money
    again), the screenshot becomes the pending image of a "which order is this payment for?" question, and the customer's pick files it.
    """
    withdrawal = await pvs.withdraw_last_proof(ctx.db, ctx.client, ctx.conv.id, reason="customer says the screenshot belongs to another order")
    if withdrawal is None:
        return await _reply(ctx, "pay_proof_change_none")
    message_id = (await ctx.db.execute(select(PaymentProof.message_id).where(PaymentProof.id == withdrawal.proof_id))).scalar_one_or_none()
    if message_id:
        ctx.conv.pending_proof_image_id = message_id
        await ctx.db.commit()
    orders = await pvs.find_open_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
    logger.info("PROOF_CHANGE conv=%s withdrawn_from=%s image=%s", ctx.conv.id, withdrawal.order_number, message_id)
    return await _ask_which_order(ctx, orders)


async def _stale_pick_reply(ctx, wanted_id: str, orders: list | None = None):
    """
    A picker button tapped when its image is already filed (conv=60: ORD-…0077 tapped after the screenshot went to ORD-…0078):
    "Your last screenshot is linked to <order>. Send a separate screenshot for <wanted>".
    """
    lang = pvs._lang(ctx.conv)
    wanted = (await ctx.db.execute(select(Order).where(Order.id == int(wanted_id) if wanted_id.isdigit() else Order.id == -1))).scalar_one_or_none()
    linked = await _latest_image_link(ctx)
    if wanted is None or linked is None:
        return await _reply(ctx, "pay_ask_screenshot")
    if linked[0] == wanted.id:
        return await _reply_text(ctx, get_template(lang, "pay_already_linked", order=wanted.order_number))
    return await _reply_text(ctx, get_template(lang, "pay_stale_pick", linked=linked[1], wanted=wanted.order_number))


async def _handle_payment_denial(ctx):
    """
    "This is product not the payment": the last image filed as a payment proof is withdrawn (audit row kept, gone from the seller's pending
    queue, the order waits for a screenshot again) and the customer is taken to the product flow. None when there is nothing to withdraw.
    """
    withdrawal = await pvs.withdraw_last_proof(ctx.db, ctx.client, ctx.conv.id)
    if withdrawal is None:
        # Nothing attached any more (the first denial already withdrew it): a repeated "no, this is not the payment" must not get the
        # payment instruction again — say plainly that nothing is filed and offer the product flow.
        orders = await pvs.find_open_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
        if not orders:
            return None
        await _save_inbound(ctx, ctx.user_text)
        return await _reply_text(ctx, get_template(pvs._lang(ctx.conv), "pay_denial_nothing", order_number=orders[0].order_number))
    logger.info("PROOF_WITHDRAWN conv=%s order=%s proof=%s reverted=%s", ctx.conv.id, withdrawal.order_number, withdrawal.proof_id, withdrawal.order_back_to_pending)
    await _save_inbound(ctx, ctx.user_text)
    lang = pvs._lang(ctx.conv)
    return await _reply_text(
        ctx, f"{get_template(lang, 'pay_proof_withdrawn', order_number=withdrawal.order_number)}\n\n{get_template(lang, 'pay_product_photo_ask')}",
    )


async def _handle_payment_status(ctx):
    """"Please confirm my payments": one line per recent order — screenshot received / waiting for verification / not paid yet / confirmed. No planner."""
    lang = pvs._lang(ctx.conv)
    orders = await pvs.recent_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
    await _save_inbound(ctx, ctx.user_text)
    if not orders:
        return await _reply_text(ctx, get_template(lang, "pay_status_none"))
    key_by_status = {ORDER_PENDING_PAYMENT: "pay_status_pending", ORDER_PAYMENT_SUBMITTED: "pay_status_submitted", ORDER_PAID: "pay_status_paid"}
    lines = [get_template(lang, key_by_status[o.status], order=o.order_number, amount=format_price(o.total_amount)) for o in orders]
    return await _reply_text(ctx, get_template(lang, "pay_status_header") + "\n" + "\n".join(lines))


async def _handle_reclassification(ctx, orders: list):
    """
    "This is payment photo" / "maine pay kar diya": the customer's own word beats our classifier. The images of the last
    ``payment_reclassify_window_minutes`` that are not filed anywhere become payment proofs (audited, ``reclassified_by_customer``); the
    order comes from the amount on the receipt (one unpaid order → that one) or the customer is asked. No image → ask for the screenshot.
    """
    window = timedelta(minutes=get_settings().payment_reclassify_window_minutes)
    images = await _unattached_images(ctx, since=window)
    await _save_inbound(ctx, ctx.user_text)
    if not images:
        return None
    targets = pending_only(orders) or orders
    amounts = await _amounts_of(images) if len(targets) > 1 else []
    order, ambiguous, auto = resolve_target(ctx, targets, amounts)
    logger.info(
        "PROOF_RECLASSIFIED conv=%s reclassified_by_customer=true images=%s order=%s ambiguous=%s",
        ctx.conv.id, [m.id for m in images], getattr(order, "order_number", None), ambiguous,
    )
    if ambiguous:
        ctx.conv.pending_proof_image_id = images[0].id
        await ctx.db.commit()
        ctx.conv.image_batch_reply_at = None            # the customer spoke: this question must not be swallowed by an earlier image burst
        return await _ask_which_order(ctx, orders)
    ctx.media_url = images[-1].media_url
    return await _attach_images(ctx, order, images, multiple_orders=len(targets) > 1, reclassified=True, auto=auto)


async def _handle_payment_message(ctx):
    """Proof / paid-word / denial / status handling for a conversation with an order awaiting payment."""
    msg_type = ctx.message.type
    is_image = msg_type == "image"
    text_like = msg_type in ("text", "interactive")
    button = ctx.btn_nonce_parsed[0] if getattr(ctx, "btn_nonce_parsed", None) else None
    if text_like and button and (button in (BTN_PROOF_PAYMENT, BTN_PROOF_PRODUCT, BTN_PROOF_CHANGE) or button.startswith(BTN_PROOF_ORDER_PREFIX)):
        return await _handle_proof_tap(ctx, button)
    if text_like and not button and pvs.is_payment_denial(ctx.user_text):
        withdrawn = await _handle_payment_denial(ctx)
        if withdrawn is not None:
            return withdrawn
    if text_like and not button and pvs.is_payment_status_request(ctx.user_text):
        return await _handle_payment_status(ctx)
    affirmation = text_like and not button and pvs.is_payment_affirmation(ctx.user_text)
    is_paid_text = text_like and (affirmation or pvs.is_paid_intent(ctx.user_text))
    if not (is_image or is_paid_text):
        return None

    orders = await pvs.find_open_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
    if not orders:
        return None  # image/"paid" with no pending order → normal flow
    order = orders[0]

    if is_image:
        if not ctx.media_url:
            # Couldn't fetch the image from Meta — don't fake a proof.
            await _save_inbound(ctx, ctx.user_text)
            if not await _claim_image_reply(ctx):
                return _silent()
            return await _reply(ctx, "pay_ask_screenshot")
        verdict = await classify_inbound_image(ctx, orders)
        if verdict.unreadable:
            # A black / blank / far-too-dark frame: nothing to read, nothing to file. Ask for the screenshot again (once per burst).
            await _save_inbound(ctx, ctx.user_text)
            if not await _claim_image_reply(ctx):
                return _silent()
            return await _reply(ctx, "pay_image_unreadable")
        if verdict.kind == payment_proof_classifier.PRODUCT_PHOTO and verdict.decisive:
            logger.info("Image while order %s awaits payment is a product photo (%s) — product flow conv=%s", order.id, verdict.decided_by, ctx.conv.id)
            ctx.image_is_product_photo = True
            ctx.image_verdict = verdict
            return None
        saved = await _save_inbound(ctx, ctx.user_text)
        if verdict.kind != payment_proof_classifier.PAYMENT_PROOF or not verdict.decisive:
            return await _ask_image_kind(ctx, first_image_id=saved.id)
        targets = pending_only(orders) or orders
        target, ambiguous, auto = resolve_target(ctx, targets, verdict.amounts)
        if ambiguous:
            if not getattr(ctx.conv, "pending_proof_image_id", None):
                ctx.conv.pending_proof_image_id = saved.id
                await ctx.db.commit()
            return await _ask_which_order(ctx, orders)
        return await _attach_images(ctx, target, [saved], multiple_orders=len(targets) > 1, auto=auto)

    if is_paid_text:
        window = timedelta(minutes=get_settings().payment_reclassify_window_minutes)
        if await _unattached_images(ctx, since=window):
            return await _handle_reclassification(ctx, orders)
    saved = await _save_inbound(ctx, ctx.user_text)       # "paid" typed/tapped with no image
    if order.status == ORDER_PENDING_PAYMENT:
        return await _reply(ctx, "pay_ask_screenshot")
    if order.status == ORDER_PAYMENT_SUBMITTED:
        # The customer typed: they get an answer every time (the throttled "verification in progress" ack applies to images only).
        if pvs.proof_ack_due(ctx.conv):
            return await _reply(ctx, "pay_proof_more", stamp_ack=True)
        return await _reply_text(ctx, get_template(pvs._lang(ctx.conv), "pay_awaiting_verification", order_number=order.order_number))
    return _silent("ack_throttled")


_ACTIVITY = {"order_collection": "collecting", "awaiting_final_confirmation": "confirming"}


async def _note_trace(ctx) -> None:
    """Report the turn in the TURN header the new way — ``stage=<what the customer is doing> pending_orders=[…]`` — never ``payment→payment``."""
    try:
        orders = await pvs.find_open_payment_orders(ctx.db, ctx.client.id, ctx.conv.id, getattr(ctx, "sender_phone", None))
        turn_trace.note(
            stage=_ACTIVITY.get(getattr(ctx.conv, "current_stage", None) or "", "browsing"),
            pending=[f"{o.order_number}:{o.status}" for o in orders],
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never break a turn
        logger.debug("trace note failed: %s", exc)


async def pre_process(ctx):
    """Run the inbound pre-hooks; returns a PipelineResult to short-circuit, else None."""
    conv, client = ctx.conv, ctx.client
    if client is None or conv is None:
        return None
    try:
        await conversation_control.maybe_auto_resume(ctx.db, client, conv)
        await _ingest_media(ctx)
        result = await _handle_payment_message(ctx)
        if result is not None:
            await _note_trace(ctx)
        return result
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
    drop a message without saving it), back-fills media_url (channel is set when the row is written; migration 0076 fixed old rows), and
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
            if getattr(conv, "is_sandbox", False):
                break       # the owner's own simulator chat must not raise inbox notifications
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
