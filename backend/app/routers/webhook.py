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
from app.services.order_pipeline import InboundContext, _decode_btn, handle_inbound_message
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


async def _get_client_by_phone_number_id(db: AsyncSession, phone_number_id: str | None):
    """
    Look up the active client whose whatsapp_phone_number_id matches the
    phone_number_id from the webhook metadata.

    phone_number_id is the stable Meta numeric ID assigned to the WhatsApp
    Business number — it never changes format unlike display_phone_number.

    Falls back to display_phone_number match (whatsapp_number column) when
    phone_number_id is absent or unrecognised, then to the first active client
    for single-tenant compatibility.

    Args:
        db:              Active async DB session.
        phone_number_id: The numeric Meta phone number ID from webhook metadata.
                         May be None if parsing failed.

    Returns:
        Matching Client instance, or None if no active client exists at all.
    """
    from app.models.client import Client

    if phone_number_id:
        result = await db.execute(
            select(Client).where(
                Client.whatsapp_phone_number_id == phone_number_id,
                Client.is_active == True,  # noqa: E712
            ).limit(1)
        )
        client = result.scalar_one_or_none()
        if client:
            return client
        logger.warning(
            "No active client found for phone_number_id=%s — falling back to first active client.",
            phone_number_id,
        )

    # Fallback: first active client (single-tenant compatibility)
    result = await db.execute(
        select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
    )
    return result.scalar_one_or_none()


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
        logger.info("Skipping unsupported message type '%s'.", message.type)
        return {"status": "ok"}

    sender_phone = message.from_

    if await _is_rate_limited(sender_phone):
        logger.warning("Rate limit: %s sending too fast — message dropped.", sender_phone)
        return {"status": "ok"}

    wamid = getattr(message, "id", None)

    # Deduplicate: Meta retries webhooks on timeout — skip if already processed.
    if wamid:
        from app.models.message import Message as MessageModel
        dup_result = await db.execute(
            select(MessageModel).where(MessageModel.wamid == wamid).limit(1)
        )
        if dup_result.scalar_one_or_none() is not None:
            logger.info("Duplicate message %s — skipping.", wamid)
            return {"status": "ok"}

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
        if message.type == "audio" and message.audio is not None:
            try:
                await outbound.send_text(
                    sender_phone,
                    "🎤 Voice note suna. Ek second...",
                    kind=MessageKind.PIPELINE_REPLY,
                    db=db,
                )
            except Exception as exc:
                logger.warning("Ack send failed for audio: %s", exc)

    # Resolve the client that owns this WhatsApp number.
    # display_phone_number in the webhook metadata is the business's number —
    # match it against client.whatsapp_number set during onboarding.
    webhook_phone_number_id: str | None = None
    try:
        meta = payload.entry[0].changes[0].value.metadata
        webhook_phone_number_id = meta.phone_number_id
    except (IndexError, AttributeError):
        pass
    client = await _get_client_by_phone_number_id(db, webhook_phone_number_id)

    try:
        conv = await conversation_service.get_or_create_conversation(
            db, sender_phone, client_id=client.id if client else None
        )
    except Exception as exc:
        logger.error("DB error creating conversation for %s: %s", sender_phone, exc)
        return {"status": "ok"}

    ctx = InboundContext(
        db=db,
        client=client,
        conv=conv,
        sender_phone=sender_phone,
        message=message,
        user_text=user_text,
        wamid=wamid,
        btn_nonce_parsed=_btn_nonce_parsed,
        download_media=vision_service.download_whatsapp_media,
        is_whatsapp=getattr(conv, "channel", "whatsapp") == "whatsapp",
    )
    result = await handle_inbound_message(ctx)

    # phone_number_id: prefer the one in the webhook metadata (most accurate),
    # fall back to the client's configured ID.
    pid = webhook_phone_number_id or (
        getattr(client, "whatsapp_phone_number_id", None) if client else None
    )

    # ── WhatsApp adapter — the ONLY place whatsapp_service is called for the
    # main reply path (button nonce encoding, send_text/button/list, the
    # fallback-to-text-on-send-failure behavior, pre_texts, and deferred
    # product images all live in the adapter).
    try:
        await send_pipeline_result(
            result, db=db, conv=conv, sender_phone=sender_phone, pid=pid,
        )
    except Exception as exc:
        logger.error("WhatsApp send error: %s", exc)

    return {"status": result.status}
