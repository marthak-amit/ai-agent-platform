"""
Meta Cloud API WhatsApp webhook router.

Handles:
- GET  /webhook  : webhook verification challenge from Meta
- POST /webhook  : incoming WhatsApp messages

Message flow:
  1. Validate X-Hub-Signature-256
  2. Parse payload → extract text message
  3. Load conversation history from DB
  4. Call Groq with history + client system prompt
  5. Save user message and AI reply to DB
  6. Tag lead (hot/warm/cold) in background
  7. Send reply via WhatsApp Cloud API
"""

import asyncio
import hashlib
import hmac
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.schemas.webhook import WhatsAppWebhookPayload
from app.services import catalogue_service, conversation_service, outbound, vision_service
from app.services.send_gate import MessageKind
from app.services import message_batcher, turn_trace
from app.services.order_pipeline import InboundContext, _decode_btn, ensure_reply, handle_inbound_message
from app.routers._whatsapp_adapter import send_pipeline_result

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# NOTE: all business-logic helpers (nonce encode/decode, slot-question
# builder, the 8 pipeline slices, and now the handle_inbound_message
# orchestrator that consolidates them) live in app/services/order_pipeline.py.
# This router only does WhatsApp-specific payload parsing, client/conversation
# resolution, and channel adapter dispatch — see handle_inbound_message's
# docstring for the exact division of responsibility.

# In-memory rate limiter: phone → list of message timestamps
# Uses asyncio.Lock so concurrent requests don't race on the shared dict.
_rate_limit_store: dict[str, list[datetime]] = defaultdict(list)
_rate_lock = asyncio.Lock()
_RATE_LIMIT_MESSAGES = 5   # max messages per window
_RATE_LIMIT_WINDOW = 10    # seconds
_RATE_LIMIT_COOLDOWN = 30  # reserved for future per-phone cooldown


async def _is_rate_limited(phone: str) -> bool:
    """Return True if *phone* has exceeded the per-window message cap."""
    async with _rate_lock:
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=_RATE_LIMIT_WINDOW)
        _rate_limit_store[phone] = [
            ts for ts in _rate_limit_store[phone] if ts > window_start
        ]
        if len(_rate_limit_store[phone]) >= _RATE_LIMIT_MESSAGES:
            return True
        _rate_limit_store[phone].append(now)
        return False


async def _send_typing_indicator(wamid: str, phone_number_id: str | None) -> None:
    """Fire-and-forget: show "typing..." to the sender of *wamid*. Never raises."""
    try:
        await outbound.send_typing_indicator(wamid, phone_number_id)
    except Exception as exc:
        logger.debug("Typing indicator send failed for wamid=%s: %s", wamid, exc)


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> str:
    """
    Handle Meta webhook verification challenge.

    Returns hub.challenge as plain text when hub.mode == 'subscribe' and
    hub.verify_token matches META_VERIFY_TOKEN from env.

    Raises:
        HTTPException 403: If mode or token do not match.
    """
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        logger.info("Webhook verification successful.")
        return hub_challenge
    logger.warning("Webhook verification failed.")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")


def _verify_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Validate the X-Hub-Signature-256 header from Meta.

    Args:
        payload_bytes:    Raw bytes of the request body.
        signature_header: Value of the X-Hub-Signature-256 header.

    Returns:
        True if the computed HMAC-SHA256 matches the header, False otherwise.
    """
    settings = get_settings()
    if not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header.removeprefix("sha256=")
    computed_sig = hmac.new(
        key=settings.meta_app_secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed_sig, expected_sig)


def _media_downloader(download, access_token):
    """Bind the business's own access token (if any) into a one-argument media downloader."""
    async def _download(media_id):
        """Download one media item with the bound per-client token."""
        return await download(media_id, access_token=access_token) if access_token else await download(media_id)

    return _download


async def _get_client_by_phone_number_id(db: AsyncSession, phone_number_id: str | None):
    """
    Look up the active client whose whatsapp_phone_number_id matches the
    phone_number_id from the webhook metadata.

    phone_number_id is the stable Meta numeric ID assigned to the WhatsApp
    Business number — it never changes format unlike display_phone_number.

    There is deliberately NO fallback: an absent, unknown or inactive
    phone_number_id returns None and the caller drops the event. Routing an
    unmapped number to "the first active client" would hand one tenant's
    customers (and their messages) to another tenant. A phone_number_id mapped
    to more than one active client is ambiguous and is also dropped.

    Args:
        db:              Active async DB session.
        phone_number_id: The numeric Meta phone number ID from webhook metadata.
                         May be None if parsing failed.

    Returns:
        The matching active Client, or None when nothing maps to this id.
    """
    from app.models.client import Client
    from app.services.tenant_routing import find_active_client_by_channel_id

    return await find_active_client_by_channel_id(
        db, Client.whatsapp_phone_number_id, phone_number_id, label="phone_number_id"
    )


# NOTE: _get_system_prompt, _record_usage, _get_catalogue_context, and
# _find_sku_matched_products moved to app/services/order_pipeline.py as part
# of the handle_inbound_message consolidation — they are channel-neutral and
# now called only from inside that orchestrator.

# NOTE: _should_use_buttons moved to app/services/order_pipeline.py (SLICE 8
# of the webhook.py strangler-fig refactor) — it's now a private helper used
# only by decide_send_instruction() there.


# NOTE: _send_bank_transfer_details moved to app/services/order_pipeline.py as
# _build_bank_transfer_text() (pure text builder) as part of the
# handle_inbound_message consolidation — the actual send now flows through
# PipelineResult.pre_texts → the WhatsApp adapter, never a direct
# whatsapp_service call from this router.


async def _is_product_out_of_stock(db: AsyncSession, product) -> bool:
    """
    Return True when the product is definitively out of stock.

    For non-variant products: product.stock <= 0.
    For variant products: True only when ALL variants have stock <= 0.
    Partial out-of-stock (some variants zero) returns False — the customer
    has not chosen their variant yet so we cannot reject the product upfront.
    """
    if product is None:
        return False
    if not getattr(product, "has_variants", False):
        return (getattr(product, "stock", None) or 0) <= 0
    from app.models.product_variant import ProductVariant as _PVCheck
    _pv_rows = await db.execute(
        select(_PVCheck).where(_PVCheck.product_id == product.id)
    )
    _pvs = list(_pv_rows.scalars().all())
    if not _pvs:
        return (getattr(product, "stock", None) or 0) <= 0
    return all((getattr(v, "stock", 0) or 0) <= 0 for v in _pvs)


async def _deduct_stock_and_alert(db: AsyncSession, order, product, client) -> None:
    """
    Deduct stock for a confirmed order and send owner alerts for low / zero stock.

    Handles both simple products and variant products. Marks order.stock_deducted
    to prevent double-deduction. All DB mutations are committed once at the end.
    """
    from app.models.product_variant import ProductVariant

    qty = order.quantity

    _order_material = getattr(order, "variant_material", None)
    if product.has_variants and (order.variant_color or order.variant_size or _order_material):
        # Find the matching variant
        stmt = select(ProductVariant).where(ProductVariant.product_id == product.id)
        if order.variant_color:
            stmt = stmt.where(ProductVariant.color == order.variant_color)
        if order.variant_size:
            stmt = stmt.where(ProductVariant.size == order.variant_size)
        if _order_material:
            stmt = stmt.where(ProductVariant.material == _order_material)
        result = await db.execute(stmt)
        variant = result.scalar_one_or_none()

        if variant:
            variant.stock = max(0, variant.stock - qty)
            # Recalculate parent stock as sum of all variant stocks
            all_variants_result = await db.execute(
                select(ProductVariant).where(ProductVariant.product_id == product.id)
            )
            all_variants = list(all_variants_result.scalars().all())
            product.stock = sum(v.stock for v in all_variants)
            stock_remaining = variant.stock
        else:
            # Variant not found — fall back to product-level deduction
            product.stock = max(0, (product.stock or 0) - qty)
            stock_remaining = product.stock
    else:
        product.stock = max(0, (product.stock or 0) - qty)
        stock_remaining = product.stock

    order.stock_deducted = True
    await db.commit()

    # Send owner alert — best-effort; never raise
    try:
        if client and client.phone:
            if stock_remaining == 0:
                alert = (
                    f"❌ Out of stock!\n"
                    f"{product.name} is now out of stock.\n"
                    f"Please restock soon."
                )
                await outbound.send_owner_text(client.phone, alert)
            elif stock_remaining <= product.low_stock_alert:
                alert = (
                    f"⚠️ Low stock alert!\n"
                    f"{product.name} — only {stock_remaining} pieces remaining."
                )
                await outbound.send_owner_text(client.phone, alert)
    except Exception as exc:
        logger.warning("Stock alert notification failed: %s", exc)


# NOTE: _reset_order_slots_after_completion moved to app/services/order_pipeline.py
# (SLICE 7 of the webhook.py strangler-fig refactor). Imported back below for
# the remaining call sites outside run_order_payment() (there are none left,
# kept import for symmetry with prior slices' pattern).


@router.post("", status_code=status.HTTP_200_OK)
async def receive_message(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle incoming WhatsApp message webhook from Meta Cloud API.

    Always returns HTTP 200 — Meta retries on any non-200 response,
    which would cause duplicate message processing.

    Args:
        request: Raw FastAPI Request (body bytes needed for signature check).
        db:      Injected async DB session.

    Returns:
        {"status": "ok"} on success or when the message is intentionally skipped.

    Raises:
        HTTPException 401: If signature validation fails.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_signature(raw_body, signature):
        logger.warning("Invalid X-Hub-Signature-256.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature.",
        )

    try:
        payload = WhatsAppWebhookPayload.model_validate_json(raw_body)
    except Exception as exc:
        logger.error("Failed to parse webhook payload: %s", exc)
        return {"status": "parse_error"}

    message = payload.get_first_message()
    if message is None:
        return {"status": "ok"}

    if message.type not in ("text", "image", "audio", "interactive"):
        logger.info("Unsupported message type '%s'.", message.type)
        if message.type in UNSUPPORTED_REPLY_TYPES:
            await _notify_unsupported(db, payload, message)
        return {"status": "ok"}

    sender_phone = message.from_

    if await _is_rate_limited(sender_phone):
        logger.warning("Rate limit: %s sending too fast — message dropped.", sender_phone)
        return {"status": "ok"}

    wamid = getattr(message, "id", None)

    # Deduplicate: Meta retries webhooks on timeout — skip if already processed.
    if wamid and message_batcher.seen_recently(wamid):
        logger.info("Duplicate message %s (already in a batch) — skipping.", wamid)
        return {"status": "ok"}
    if wamid and wamid in _inflight_wamids:
        logger.info("Duplicate message %s (still being processed) — skipping.", wamid)
        return {"status": "ok"}
    if wamid:
        from app.models.message import Message as MessageModel
        dup_result = await db.execute(
            select(MessageModel).where(MessageModel.wamid == wamid).limit(1)
        )
        if dup_result.scalar_one_or_none() is not None:
            logger.info("Duplicate message %s — skipping.", wamid)
            return {"status": "ok"}

    settings = get_settings()
    if settings.webhook_background_processing:
        # Answer Meta now: it retries any webhook that takes longer than a few seconds, and a turn can wait on the LLM.
        if wamid:
            _inflight_wamids.add(wamid)
        try:
            pnid = payload.entry[0].changes[0].value.metadata.phone_number_id
        except (IndexError, AttributeError):
            pnid = None
        ticket = _take_ticket((pnid, sender_phone))          # synchronously, so the order of arrival is the order of service
        task = asyncio.create_task(_process_in_background(payload, message, wamid, ticket))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"status": "accepted"}
    return await _process_message(db, payload, message, wamid)


# Message types the bot cannot read but a customer expects an answer to (a reaction / system event gets none).
UNSUPPORTED_REPLY_TYPES = frozenset({"unsupported", "document", "video", "location", "contacts", "order"})
_unsupported_notified: dict[tuple, float] = {}        # (client id, sender) → monotonic time of the last notice (in-process, like the rate limiter)


def _unsupported_due(key: tuple, now: float, window_seconds: float) -> bool:
    """True when this sender has not been told "I can't open that" within the window; records the notice when due."""
    last = _unsupported_notified.get(key)
    if last is not None and now - last < window_seconds:
        return False
    _unsupported_notified[key] = now
    if len(_unsupported_notified) > 5000:                      # forget old entries
        for k in [k for k, t in _unsupported_notified.items() if now - t >= window_seconds]:
            _unsupported_notified.pop(k, None)
    return True


async def _notify_unsupported(db: AsyncSession, payload, message) -> None:
    """
    Tell the customer once per ``unsupported_notice_minutes`` that this kind of message cannot be opened ("send text, a photo or a voice
    note"), in their language. Silent for numbers that map to no tenant, tenants that are not live, and chats a human has taken over.
    Never raises: the webhook must still answer Meta.
    """
    import time

    from app.services.language_templates import get_template

    try:
        pnid = payload.entry[0].changes[0].value.metadata.phone_number_id
    except (IndexError, AttributeError):
        return
    try:
        client = await _get_client_by_phone_number_id(db, pnid)
        if client is None or getattr(client, "bot_active", True) is False:
            return
        sender = message.from_
        if not _unsupported_due((client.id, sender), time.monotonic(), get_settings().unsupported_notice_minutes * 60.0):
            return
        conv = await conversation_service.get_or_create_conversation(db, sender, client_id=client.id)
        if conv.ai_enabled is False:
            return
        text = get_template(getattr(conv, "last_customer_language", None) or "english", "unsupported_message")
        await outbound.send_text(
            sender, text, kind=MessageKind.PIPELINE_REPLY, db=db, client_id=client.id, conversation_id=conv.id,
            phone_number_id=pnid, access_token=getattr(client, "whatsapp_access_token", None),
        )
        await conversation_service.save_message(db, conv.id, "assistant", text, channel=conv.channel, sender_type="bot")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unsupported-type notice failed: %s", exc)


# ── background processing ────────────────────────────────────────────────────

_background_tasks: set[asyncio.Task] = set()          # strong references until each task is done
_inflight_wamids: set[str] = set()                    # accepted but not finished: a Meta retry of these is dropped
_last_done: dict[tuple, asyncio.Future] = {}          # per sender: the most recently accepted message's completion


@dataclass
class _Ticket:
    """A message's place in its sender's queue: it may run its turn only after every earlier message of that sender finished."""

    prev: "asyncio.Future | None"
    done: asyncio.Future

    async def wait_turn(self) -> None:
        """Wait until the previous message of this sender is completely finished."""
        if self.prev is not None:
            await asyncio.shield(self.prev)

    async def finish(self) -> None:
        """Mark this message finished — but never before its predecessor (a batched follower returns early)."""
        try:
            await self.wait_turn()
        except Exception:  # noqa: BLE001 — the predecessor's failure is its own
            pass
        if not self.done.done():
            self.done.set_result(None)


def _take_ticket(key: tuple) -> _Ticket:
    """Join the end of this sender's queue (call synchronously on accept: the order of arrival is the order of service)."""
    prev = _last_done.get(key)
    if prev is not None and prev.done():
        prev = None
    done = asyncio.get_running_loop().create_future()
    _last_done[key] = done
    done.add_done_callback(lambda f, k=key: _last_done.pop(k, None) if _last_done.get(k) is f else None)
    return _Ticket(prev=prev, done=done)


def _background_session_factory():
    """The session factory background tasks use (their own session: the request's is closed once the 200 is sent)."""
    from app.db import _get_session_factory

    return _get_session_factory()


async def _process_in_background(payload, message, wamid: str | None, ticket: _Ticket) -> None:
    """Run ``_process_message`` with a fresh DB session; never raises (nobody is waiting for the result)."""
    try:
        async with _background_session_factory()() as db:
            await _process_message(db, payload, message, wamid, ticket)
    except Exception:
        logger.exception("Background processing of message %s failed", wamid)
    finally:
        await ticket.finish()
        if wamid:
            _inflight_wamids.discard(wamid)


async def wait_for_background_tasks() -> None:
    """Wait until every accepted message has been processed (tests; also useful for a graceful shutdown)."""
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


async def _process_message(db: AsyncSession, payload, message, wamid: str | None, ticket: "_Ticket | None" = None) -> dict:
    """Everything after verify / parse / rate-limit / dedupe: tenant lookup, typing indicator, batching, the turn, the reply."""
    sender_phone = message.from_
    # Resolve phone_number_id from webhook metadata now (pure in-memory parse,
    # no I/O) so the typing indicator can fire before any DB/LLM/OCR work.
    webhook_phone_number_id: str | None = None
    try:
        webhook_phone_number_id = payload.entry[0].changes[0].value.metadata.phone_number_id
    except (IndexError, AttributeError):
        pass

    # Resolve the owning tenant BEFORE any outbound side effect (typing indicator,
    # voice-note ack): an unmapped phone_number_id must be dropped, never answered
    # on another tenant's behalf or with the global fallback credentials.
    client = await _get_client_by_phone_number_id(db, webhook_phone_number_id)
    if client is None:
        return {"status": "unmapped_number"}

    # Show "typing..." as early as possible — only for messages that survived
    # rate-limit + dedup and are about to enter real processing. Fire-and-
    # forget: never awaited, never allowed to delay or break the reply.
    live = getattr(client, "bot_active", True) is not False      # not live yet ("Go live" not pressed): stay completely silent
    if wamid and live:
        asyncio.create_task(_send_typing_indicator(wamid, webhook_phone_number_id))

    # Stash nonce parsed from interactive button IDs; validated after conv is loaded.
    _btn_nonce_parsed: tuple[str, str, str] | None = None

    if message.type == "text" and message.text is not None:
        user_text = message.text.body
        logger.info("Text message from %s: %s", sender_phone, user_text)
    elif message.type == "interactive" and message.interactive is not None:
        # Button or list reply — map known button IDs to canonical text so the
        # stage machine fires correctly regardless of button title wording.
        # Typed fallback ("cancel"/"paid") also continues to work.
        interactive = message.interactive
        if interactive.button_reply is not None:
            _btn_id = (interactive.button_reply.id or "").lower()
            # BUG 4 FIX: decode nonce-encoded button IDs.
            # New format: "{action}~{conv_id}~{nonce}".  Extract the action so the
            # existing checks below work unchanged, and stash nonce for validation.
            _decoded = _decode_btn(_btn_id)
            if _decoded is not None:
                _btn_id = _decoded[0]  # action only
                _btn_nonce_parsed = _decoded
            if _btn_id == "confirm_pay":
                # "confirm" is in _CONFIRMATION_YES in conversation_flow — avoids
                # colliding with quantity answer "1".
                user_text = "confirm"
            elif _btn_id in ("cancel_order", "confirm_no"):
                user_text = "cancel"
            elif _btn_id == "paid_done":
                user_text = "paid"
            elif _btn_id == "offer_yes":
                # E2: single-product offer "Yes" button — maps to the existing
                # purchase-affirmation path (conversation_flow._PURCHASE_AFFIRMATION).
                user_text = "yes"
            elif _btn_id == "offer_no":
                # E2: single-product offer "No" button — keep browsing.
                user_text = "no"
            elif _btn_id in ("cancel_yes", "cancel_no"):
                # "Cancel this order?" Yes/No: the id is the answer (the label is translated).
                user_text = "yes" if _btn_id == "cancel_yes" else "no"
            elif _btn_id == "switch_product":
                user_text = "switch"
            elif _btn_id == "add_both":
                user_text = "add both"
            elif _btn_id == "keep_current":
                user_text = "keep current"
            elif _btn_id == "add_crosssell":
                # Order-summary option "2️⃣ Add <product> too" as a button.
                user_text = "2"
            elif _btn_id == "addr_change":
                # Saved-address offer "Change" button: the id is the answer (the label is translated).
                user_text = "change"
            elif _btn_id in ("upi", "cod", "bank_transfer"):
                # Payment-method buttons: the id is the answer; the title is translated per customer
                # language, so it must never be parsed as the customer's text.
                user_text = {"upi": "UPI", "cod": "COD", "bank_transfer": "bank transfer"}[_btn_id]
            elif catalogue_service.SKU_PATTERN.fullmatch(_btn_id.upper()):
                # E2: multi-option choice button — id IS the SKU, so resolve it
                # directly (the existing pending_choice_skus SKU-substring match
                # below picks this up with no fuzzy matching needed).
                user_text = _btn_id.upper()
            else:
                user_text = interactive.button_reply.title
        elif interactive.list_reply is not None:
            _list_id_raw = (interactive.list_reply.id or "").lower()
            # BUG 3 FIX: list row ids are now nonce-encoded too (see _sends_buttons
            # for "choice_list") — decode the same way as button replies so a stale
            # list selection can be rejected by the nonce check below.
            _list_decoded = _decode_btn(_list_id_raw)
            if _list_decoded is not None:
                _list_id = _list_decoded[0].upper()
                _btn_nonce_parsed = _list_decoded
            else:
                _list_id = _list_id_raw.upper()
            if catalogue_service.SKU_PATTERN.fullmatch(_list_id):
                # E2: list-message row id is also a SKU — resolve directly.
                user_text = _list_id
            else:
                user_text = interactive.list_reply.title
        else:
            user_text = ""
        logger.info(
            "Interactive reply from %s: type=%s text=%r",
            sender_phone,
            interactive.type,
            user_text,
        )
    elif message.type == "image":
        user_text = "[image]"
    else:
        user_text = "[voice note]"
        # Immediate acknowledgement, sent here (not via the orchestrator's
        # PipelineResult) so the customer isn't left waiting in silence while
        # transcription + the rest of the pipeline runs — a single end-of-turn
        # PipelineResult can't reproduce this "send now, continue processing"
        # timing. This is the one channel-specific send that stays inline.
        # Guard mirrors the original inline `elif message.type == "audio" and
        # message.audio is not None` — no ack for a malformed audio payload.
        if message.type == "audio" and message.audio is not None and live:
            try:
                # Emoji-only on purpose: the customer's language is not known yet (the voice note is not
                # transcribed), and a fixed Hinglish line was shown to Gujarati / Tamil / English speakers.
                await outbound.send_text(
                    sender_phone,
                    "🎤⏳",
                    kind=MessageKind.PIPELINE_REPLY,
                    db=db,
                )
            except Exception as exc:
                logger.warning("Ack send failed for audio: %s", exc)

    # Message batching: a burst of text messages ("I want" / "to buy" / "banarasi silk sadi") is answered once.
    # A follower just joins the leader's batch and returns; the leader waits out the quiet window below.
    settings = get_settings()
    from app.services import agent_v3, catalogue_snapshot

    v3 = agent_v3.enabled(client)           # agent-v3 tenants use shorter windows and do not hold messages answered for ₹0
    batch_key = ("whatsapp", client.id, sender_phone)
    batch_window = float((settings.agent_batch_window_seconds if v3 else settings.message_batch_window_seconds) or 0)
    # The suite turns batching off with MESSAGE_BATCH_WINDOW_SECONDS=0; that switch wins for every tenant.
    if float(getattr(settings, "message_batch_window_seconds", 0) or 0) <= 0:
        batch_window = 0.0
    short_window = float(settings.agent_batch_short_window_seconds if v3 else settings.message_batch_short_window_seconds)
    extended_window = float(settings.agent_batch_extended_window_seconds if v3 else settings.message_batch_extended_window_seconds)
    is_plain_text = message.type == "text" and message.text is not None
    if batch_window > 0 and is_plain_text and message_batcher.try_join(
        batch_key, user_text, wamid, max_messages=settings.message_batch_max_messages,
    ):
        return {"status": "batched"}

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_phone, client_id=client.id
        )
    except Exception as exc:
        logger.error("DB error creating conversation for %s: %s", sender_phone, exc)
        return {"status": "ok"}


    lead = batch_window > 0 and is_plain_text and message_batcher.eligible(
        conv, user_text, skip_social=v3, product_names=catalogue_snapshot.product_names(client.id) if v3 else None,
    )
    async with message_batcher.turn(
        batch_key, enabled=batch_window > 0, lead=lead, text=user_text, wamid=wamid, window=batch_window,
        short_window=short_window, extended_window=extended_window,
        max_wait=float(settings.message_batch_max_wait_seconds), max_messages=int(settings.message_batch_max_messages),
    ) as batch_turn:
        if batch_turn.waited > 0.05:
            try:
                await db.refresh(conv)      # an earlier turn of this sender may have changed the conversation meanwhile
            except Exception as exc:
                logger.warning("conv refresh after batching failed conv=%s: %s", getattr(conv, "id", None), exc)
        if batch_turn.text != user_text:
            user_text = batch_turn.text
            try:
                message.text.body = user_text   # keep the message object consistent with the merged text
            except Exception:
                pass
        if ticket is not None:
            await ticket.wait_turn()       # earlier messages of this sender (background mode) are completely done first
        return await _run_turn(
            db, client, conv, sender_phone, message, user_text, wamid, _btn_nonce_parsed,
            webhook_phone_number_id, batch_turn.waited,
        )


async def _run_turn(
    db: AsyncSession, client, conv, sender_phone: str, message, user_text: str, wamid: str | None,
    btn_nonce_parsed, webhook_phone_number_id: str | None, reply_delay_credit: float = 0.0,
) -> dict:
    """Run the pipeline for one (possibly merged) customer turn and send the reply through the WhatsApp adapter."""
    ctx = InboundContext(
        db=db,
        client=client,
        conv=conv,
        sender_phone=sender_phone,
        message=message,
        user_text=user_text,
        wamid=wamid,
        btn_nonce_parsed=btn_nonce_parsed,
        download_media=_media_downloader(vision_service.download_whatsapp_media, getattr(client, "whatsapp_access_token", None)),
        is_whatsapp=getattr(conv, "channel", "whatsapp") == "whatsapp",
        reply_delay_credit=reply_delay_credit,
        defer_trace=True,      # the TURN block is printed below, after the send, so wa_send / image_send are in it
    )
    try:
        result = await handle_inbound_message(ctx)
    except Exception:
        logger.exception("Pipeline crashed conv=%s — answering with the deterministic fallback", conv.id)
        try:
            await db.rollback()
            await db.refresh(conv)      # the rollback expired the rows; reload before the fallback reads them
            await db.refresh(client)
        except Exception:
            pass
        result = None
    # Every inbound message gets exactly one reply: logs `NO_REPLY conv=…` and substitutes a stage-aware
    # fallback if the pipeline ended with nothing to send (intentional silence — skip_send — is respected).
    result = await ensure_reply(result, ctx)

    # phone_number_id: prefer the one in the webhook metadata (most accurate),
    # fall back to the client's configured ID.
    pid = webhook_phone_number_id or (
        getattr(client, "whatsapp_phone_number_id", None) if client else None
    )

    from app.db import release_connection

    await release_connection(db)      # the send is a network round trip (and may wait for the 24h window / images): no pooled connection is held for it

    # ── WhatsApp adapter — the ONLY place whatsapp_service is called for the
    # main reply path (button nonce encoding, send_text/button/list, the
    # fallback-to-text-on-send-failure behavior, pre_texts, and deferred
    # product images all live in the adapter).
    try:
        await send_pipeline_result(
            result, db=db, conv=conv, sender_phone=sender_phone, pid=pid,
            access_token=getattr(client, "whatsapp_access_token", None) if client else None,
        )
    except Exception as exc:
        logger.error("WhatsApp send error: %s", exc)
    finally:
        await turn_trace.emit_pending()

    return {"status": result.status}
