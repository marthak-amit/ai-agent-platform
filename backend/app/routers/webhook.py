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
import random
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.schemas.webhook import WhatsAppWebhookPayload
from app.services import catalogue_service, conversation_service, customer_service, escalation_service, gemini_service, intent_service, lead_service, order_service, usage_service, vision_service, voice_service, whatsapp_service
from app.services import conversation_flow, language_service as _lang_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

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


def _get_system_prompt(client) -> str | None:
    """
    Return the Gemini system prompt for the resolved client, or None.

    Args:
        client: Active Client ORM instance, or None.

    Returns:
        gemini_system_prompt string, or None if no client.
    """
    return client.gemini_system_prompt if client else None


async def _record_usage(db: AsyncSession, client) -> None:
    """
    Record one message in UsageLog for the resolved client.

    Kept as a standalone helper so tests can patch it independently of the
    rest of the webhook pipeline.

    Args:
        db:     Active async DB session.
        client: Active Client ORM instance, or None (no-op when None).
    """
    if client:
        await usage_service.record_message(db, client)


async def _get_catalogue_context(
    db: AsyncSession, client, user_text: str, conv=None
) -> str | None:
    """
    Build catalogue context for Gemini from a customer message.

    Strategy (priority order):
      1. If conv.pending_product_sku is set, load that product first — ensures the
         agent always discusses the same product the customer originally asked about,
         even when the customer's reply is a short/ambiguous follow-up.
      2. Scan user_text for SKU-like tokens (e.g. SR27754). If found, look up those
         products by exact SKU.
      3. Keyword search across the full catalogue, top 5 most relevant products.

    Args:
        db:        Active async DB session.
        client:    Active Client ORM instance, or None.
        user_text: The customer's message text used as the search query.
        conv:      Conversation ORM instance (optional) — used for SKU pinning.

    Returns:
        Formatted catalogue string, or None if no relevant products found.
    """
    if client is None:
        return None

    # ── Pinned SKU (from a previous turn) ────────────────────────────────────
    pinned_sku = getattr(conv, "pending_product_sku", None) if conv else None
    if pinned_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
        if pinned:
            return catalogue_service.format_catalogue_context([pinned])

    # ── SKU-first lookup ──────────────────────────────────────────────────────
    skus = catalogue_service.extract_skus_from_text(user_text)
    if skus:
        sku_products = []
        for sku in skus:
            p = await catalogue_service.find_product_by_sku(db, client.id, sku)
            if p:
                sku_products.append(p)
        if sku_products:
            return catalogue_service.format_catalogue_context(sku_products)

    # ── Keyword fallback ──────────────────────────────────────────────────────
    products = await catalogue_service.list_products(db, client.id)
    relevant = catalogue_service.search_products(products, user_text)
    if not relevant:
        return None
    return catalogue_service.format_catalogue_context(relevant)


async def _find_sku_matched_products(db: AsyncSession, client, user_text: str) -> list:
    """
    Return the catalogue products whose SKU is explicitly quoted in the message.

    Standalone helper (separate from _get_catalogue_context) so the webhook can
    send a product photo for an exact SKU match — Learning 4: "when a customer
    quotes a SKU, send the product image first, then the text details" — without
    re-running the SKU scan or changing _get_catalogue_context's contract.

    Args:
        db:        Active async DB session.
        client:    Active Client ORM instance, or None.
        user_text: The customer's message text.

    Returns:
        List of matching Product instances (possibly empty).
    """
    if client is None:
        return []
    skus = catalogue_service.extract_skus_from_text(user_text)
    if not skus:
        return []
    matched = []
    for sku in skus:
        p = await catalogue_service.find_product_by_sku(db, client.id, sku)
        if p:
            matched.append(p)
    return matched


def _should_use_buttons(stage: str, ai_reply: str, accepts_cod: bool) -> str:
    """
    Decide which interactive message type (if any) to use for an AI reply.

    Returns one of:
      "payment_buttons"  — show UPI (+ COD if client accepts it)
      "confirm_buttons"  — show Confirm Order / Cancel
      "text"             — plain text (default)

    Only fires during order_collection to avoid cluttering casual chat.
    The payment_buttons variant respects accepts_cod — when False only
    the UPI button is shown (handled at call site, not here).
    """
    if stage != "order_collection":
        return "text"

    lower = ai_reply.lower()

    # Payment method prompt
    if any(kw in lower for kw in ("upi", "cod", "cash on delivery", "payment", "bhugtan", "payment mode")):
        return "payment_buttons"

    # Order confirmation prompt
    if any(kw in lower for kw in ("confirm", "book", "place order", "order confirm", "pakka")):
        return "confirm_buttons"

    return "text"


async def _send_upi_qr(
    sender_phone: str,
    amount_inr: float,
    order_number: str,
    client,
    pid: str | None,
) -> None:
    """
    Generate a Razorpay QR code and send it to the customer via WhatsApp.

    Falls back to sending the client's plain UPI ID if Razorpay is not
    configured.  All errors are swallowed — payment collection must never
    crash the order flow.
    """
    from app.config import get_settings as _gs
    settings = _gs()

    razorpay_ready = bool(
        settings.razorpay_key_id and settings.razorpay_key_secret
    )

    if razorpay_ready:
        try:
            from app.services import razorpay_service

            qr_data = await razorpay_service.create_qr_code(
                amount=int(amount_inr * 100),  # paise
                description=f"Order {order_number}",
                phone_number=sender_phone,
            )
            image_url = qr_data.get("image_url") or qr_data.get("short_url")
            if image_url:
                await whatsapp_service.send_image_message(
                    to_phone_number=sender_phone,
                    image_url=image_url,
                    caption=(
                        f"Scan this QR to pay ₹{amount_inr:.0f} for order {order_number}.\n"
                        f"Order will be dispatched after payment is confirmed. 🙏"
                    ),
                )
                return
        except Exception as exc:
            logger.warning("Razorpay QR generation failed, falling back to UPI text: %s", exc)

    # Fallback: send plain UPI ID text
    upi_id = getattr(client, "upi_id", None) if client else None
    if upi_id:
        await whatsapp_service.send_text_message(
            to_phone_number=sender_phone,
            message_text=(
                f"Please pay ₹{amount_inr:.0f} via UPI to complete your order {order_number}.\n"
                f"UPI ID: {upi_id}\n\n"
                f"Send payment screenshot after paying. Order ships after confirmation. 🙏"
            ),
        )
    else:
        logger.warning(
            "No Razorpay config and no UPI ID for client %s — cannot send payment request.",
            getattr(client, "id", "?"),
        )


async def _deduct_stock_and_alert(db: AsyncSession, order, product, client) -> None:
    """
    Deduct stock for a confirmed order and send owner alerts for low / zero stock.

    Handles both simple products and variant products. Marks order.stock_deducted
    to prevent double-deduction. All DB mutations are committed once at the end.
    """
    from app.models.product_variant import ProductVariant

    qty = order.quantity

    if product.has_variants and (order.variant_color or order.variant_size):
        # Find the matching variant
        stmt = select(ProductVariant).where(ProductVariant.product_id == product.id)
        if order.variant_color:
            stmt = stmt.where(ProductVariant.color == order.variant_color)
        if order.variant_size:
            stmt = stmt.where(ProductVariant.size == order.variant_size)
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
                await whatsapp_service.send_text_message(client.phone, alert)
            elif stock_remaining <= product.low_stock_alert:
                alert = (
                    f"⚠️ Low stock alert!\n"
                    f"{product.name} — only {stock_remaining} pieces remaining."
                )
                await whatsapp_service.send_text_message(client.phone, alert)
    except Exception as exc:
        logger.warning("Stock alert notification failed: %s", exc)


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

    if message.type == "text" and message.text is not None:
        user_text = message.text.body
        logger.info("Text message from %s: %s", sender_phone, user_text)
    elif message.type == "interactive" and message.interactive is not None:
        # Button or list reply — treat the tapped title as the customer's text
        interactive = message.interactive
        if interactive.button_reply is not None:
            user_text = interactive.button_reply.title
        elif interactive.list_reply is not None:
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

    # Resolve the client that owns this WhatsApp number.
    # display_phone_number in the webhook metadata is the business's number —
    # match it against client.whatsapp_number set during onboarding.
    display_phone: str | None = None
    webhook_phone_number_id: str | None = None
    try:
        meta = payload.entry[0].changes[0].value.metadata
        display_phone = meta.display_phone_number
        webhook_phone_number_id = meta.phone_number_id
    except (IndexError, AttributeError):
        pass
    client = await _get_client_by_phone_number_id(db, webhook_phone_number_id)

    try:
        conv = await conversation_service.get_or_create_conversation(db, sender_phone)
    except Exception as exc:
        logger.error("DB error creating conversation for %s: %s", sender_phone, exc)
        return {"status": "ok"}

    # Upsert customer profile — best-effort; never block the message flow.
    if client:
        try:
            customer = await customer_service.upsert_customer(
                db,
                client_id=client.id,
                phone=sender_phone,
            )
            await db.commit()
            # Block listed customers get no AI reply
            if customer.is_blocked:
                logger.info("Blocked customer %s — message dropped.", sender_phone)
                return {"status": "ok"}
        except Exception as exc:
            logger.warning("Customer upsert failed: %s", exc)

    # Human takeover: save message silently, skip AI entirely.
    if conv.ai_enabled is False:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        logger.info(
            "AI paused for %s — human takeover active, message saved silently.",
            sender_phone,
        )
        return {"status": "ok"}

    settings = get_settings()
    delay = random.uniform(settings.min_reply_delay, settings.max_reply_delay)
    await asyncio.sleep(delay)

    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]
    catalogue_context = await _get_catalogue_context(db, client, user_text, conv=conv)

    # Learning 4: when a customer quotes a SKU in a text message, send the
    # product photo first (if one exists) — the AI's text reply follows
    # separately. We deliberately do NOT do this for every message (Learning 1:
    # never send unsolicited images) — only on an explicit SKU match.
    if message.type == "text" and user_text:
        sku_products = await _find_sku_matched_products(db, client, user_text)
        # Pin the matched SKU so subsequent short replies (e.g. "yes", "ok") still
        # refer to the same product rather than triggering a fresh keyword search.
        # Guard: once in order_collection or payment, NEVER change the pinned SKU
        # — a casual product question mid-order must not derail the active order.
        _active_order_stages = {"order_collection", "payment"}
        _can_switch_sku = (
            not getattr(conv, "pending_product_sku", None)
            or (conv.current_stage or "greeting") not in _active_order_stages
        )
        if sku_products and _can_switch_sku:
            first_sku = getattr(sku_products[0], "sku", None)
            if first_sku:
                try:
                    await conversation_service.update_order_field(
                        db, conv.id, "pending_product_sku", first_sku
                    )
                    conv.pending_product_sku = first_sku
                except Exception as exc:
                    logger.error("SKU pin error: %s", exc)
        for p in sku_products:
            if getattr(p, "image_url", None):
                try:
                    await whatsapp_service.send_image_message(
                        to_phone_number=sender_phone,
                        image_url=p.image_url,
                        caption=f"{p.name} — ₹{p.price}",
                    )
                except Exception as exc:
                    logger.error("Product image send error: %s", exc)

    # ── Sales pipeline: intent → stage → focused prompt ───────────────────────
    intent = intent_service.detect_intent(user_text)
    # Pass previous language so ambiguous single-word replies ("yes", "COD")
    # inherit the customer's established language rather than resetting.
    previous_language = getattr(conv, "last_customer_language", None) or "english"
    language = _lang_svc.detect_language(user_text, previous_language=previous_language)

    # Detect conversation stage — pass stored stage so payment/completed are locked
    stage = conversation_flow.detect_stage(
        history_dicts, user_text, stored_stage=conv.current_stage
    )

    # ── Resolve pinned product and its variant info ───────────────────────────
    # Done before extract_order_field so color/size extraction knows which
    # options are valid for this specific product.
    pinned_product = None
    variant_info: dict = {"has_variants": False, "needs_color": False, "needs_size": False,
                          "available_colors": [], "available_sizes": []}
    if client:
        pinned_sku = getattr(conv, "pending_product_sku", None)
        if pinned_sku:
            try:
                pinned_product = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
                if pinned_product:
                    variant_info = await catalogue_service.get_product_variant_info(db, pinned_product)
            except Exception as exc:
                logger.error("Variant info fetch error: %s", exc)

    # ── Parse pre-filled catalogue order messages ─────────────────────────────
    # When customer taps "Order" on the public catalogue a structured message
    # like "Product: Designer Lehenga\nSKU: LH10042\nColor: Red\nSize: XL" is
    # sent. Extract and persist those fields immediately so the agent skips the
    # questions that were already answered by the catalogue form.
    if client and stage == "order_collection":
        import re as _re
        _pre_sku = _re.search(r"SKU:\s*(\S+)", user_text)
        _pre_color = _re.search(r"Color:\s*(.+)", user_text)
        _pre_size = _re.search(r"Size:\s*(.+)", user_text)
        if _pre_sku and not getattr(conv, "pending_product_sku", None):
            _sku_val = _pre_sku.group(1).strip()
            try:
                await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _sku_val)
                conv.pending_product_sku = _sku_val
                pinned_product = await catalogue_service.find_product_by_sku(db, client.id, _sku_val)
                if pinned_product:
                    variant_info = await catalogue_service.get_product_variant_info(db, pinned_product)
            except Exception as exc:
                logger.error("Pre-fill SKU error: %s", exc)
        if _pre_color and not getattr(conv, "selected_color", None):
            try:
                _color_val = _pre_color.group(1).strip()
                await conversation_service.update_order_field(db, conv.id, "selected_color", _color_val)
                conv.selected_color = _color_val
            except Exception as exc:
                logger.error("Pre-fill color error: %s", exc)
        if _pre_size and not getattr(conv, "selected_size", None):
            try:
                _size_val = _pre_size.group(1).strip()
                await conversation_service.update_order_field(db, conv.id, "selected_size", _size_val)
                conv.selected_size = _size_val
            except Exception as exc:
                logger.error("Pre-fill size error: %s", exc)

    # During order_collection, persist whichever field the customer's reply
    # most likely answers so progress survives across turns and the agent
    # never re-asks an already-answered question.
    if stage == "order_collection":
        extracted = conversation_flow.extract_order_field(conv, user_text, variant_info=variant_info)
        if extracted:
            field, value = extracted
            try:
                await conversation_service.update_order_field(db, conv.id, field, value)
                setattr(conv, field, value)
            except Exception as exc:
                logger.error("Order field update error: %s", exc)
        # Persist payment method if customer specifies COD or UPI
        if not conv.payment_method:
            upper = user_text.upper()
            if "COD" in upper or "CASH" in upper:
                try:
                    await conversation_service.update_order_field(db, conv.id, "payment_method", "COD")
                    conv.payment_method = "COD"
                except Exception:
                    pass
            elif "UPI" in upper or "GPAY" in upper or "PAYTM" in upper or "PHONEPE" in upper:
                try:
                    await conversation_service.update_order_field(db, conv.id, "payment_method", "UPI")
                    conv.payment_method = "UPI"
                except Exception:
                    pass

    # Fetch all catalogue products for this client (for master prompt)
    catalogue_products: list = []
    if client:
        try:
            catalogue_products = await catalogue_service.list_products(db, client.id)
        except Exception:
            pass

    # Check repeat customer history
    customer_history: dict | None = None
    if client:
        try:
            customer_history = await conversation_service.get_customer_history(
                db, sender_phone, client.id
            )
        except Exception:
            pass

    # Fetch full customer profile for richer personalisation
    customer_profile = None
    if client:
        try:
            customer_profile = await customer_service.get_customer(
                db, client_id=client.id, phone=sender_phone
            )
        except Exception:
            pass

    # Search knowledge base for proven answers relevant to this message
    kb_context = ""
    if client:
        try:
            from app.services import knowledge_service
            kb_entries = await knowledge_service.search_knowledge(
                client_id=client.id,
                query=user_text,
                db=db,
            )
            if kb_entries:
                kb_context = "\nPROVEN ANSWERS FROM PAST CUSTOMERS:\n"
                for entry in kb_entries:
                    kb_context += f"Q: {entry.question}\nA: {entry.answer}\n\n"
                kb_context += "Use these answers when relevant.\n"
                # Bump usage counter best-effort
                try:
                    for entry in kb_entries:
                        entry.usage_count += 1
                    await db.commit()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("KB search failed: %s", exc)

    # Fetch dynamic few-shot examples from recent successful orders.
    if client:
        try:
            from app.services import learning_service
            live_examples = await learning_service.get_live_examples_for_prompt(
                client_id=client.id,
                current_message=user_text,
                db=db,
            )
            if live_examples:
                kb_context = kb_context + "\n" + live_examples if kb_context else live_examples
        except Exception as exc:
            logger.warning("Learning service failed: %s", exc)

    # Build customer_context string for prominent prompt injection.
    # customer_profile was fetched above; build a structured block for returning customers.
    customer_context = ""
    if customer_profile and (customer_profile.total_orders or 0) > 0:
        _cp = customer_profile
        customer_context = f"""RETURNING CUSTOMER — USE THIS DATA:
Name: {_cp.name or 'Unknown'}
Previous orders: {_cp.total_orders}
Total spent: ₹{int(_cp.total_spent or 0):,}
Saved address: {_cp.address or 'None'}
Preferred language: {_cp.preferred_language}
VIP: {_cp.is_vip}

RULES:
- Greet by name: "Welcome back {_cp.name}!" (or Hindi/Gujarati equivalent)
- Address saved → confirm, don't ask: "Deliver to {_cp.address}? (yes/change)"
- Name saved → don't ask name again
- Match preferred_language always"""
    else:
        customer_context = "New customer."

    # Build master sales system prompt (overrides old single-string prompt).
    # accepts_cod and upi_id are read from client inside build_master_system_prompt.
    if client:
        system_prompt = gemini_service.build_master_system_prompt(
            client=client,
            products=catalogue_products,
            conversation_stage=stage,
            language=language,
            customer_history=customer_history,
            conversation=conv,
            customer_profile=customer_profile,
            variant_info=variant_info,
            kb_context=kb_context,
            customer_context=customer_context,
        )
    else:
        system_prompt = _get_system_prompt(client)

    # ── Escalation / prompt-injection guard ──────────────────────────────────
    _EMPATHY_REPLIES = {
        "customer_escalation": (
            "I understand your concern. "
            "Our team will contact you shortly to resolve this personally."
        ),
        "repeated_question": (
            "I'm sorry for the confusion. "
            "Let me connect you with our team for better assistance."
        ),
        "prompt_injection": None,  # handled separately below
    }

    try:
        escalation = await escalation_service.check_escalation_needed(
            user_text, history_dicts, conv.id
        )
        if escalation["escalate"]:
            reason = escalation.get("reason", "")
            logger.info("Escalation: %s from %s", reason, sender_phone)

            if reason == "prompt_injection":
                # Bypass AI entirely — send canned safe reply.
                business_name = getattr(client, "business_name", "this business") if client else "this business"
                safe_reply = (
                    f"I can only help with {business_name} products and orders. "
                    "How can I assist you?"
                )
                logger.warning("Injection attempt: %s", sender_phone)
                try:
                    await whatsapp_service.send_text_message(sender_phone, safe_reply)
                except Exception as exc:
                    logger.error("Failed to send injection-safe reply: %s", exc)
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", safe_reply)
                return {"status": "ok"}

            if escalation.get("notify_owner"):
                # Notify owner, pause AI, send one empathy message, then stop.
                try:
                    await escalation_service.notify_owner_escalation(client, conv, reason, db)
                except Exception as exc:
                    logger.error("Owner notification failed: %s", exc)

                # Pause AI — owner takes over manually.
                try:
                    from app.models.conversation import Conversation as _Conv
                    from sqlalchemy import select as _sel
                    _cr = await db.execute(_sel(_Conv).where(_Conv.id == conv.id).limit(1))
                    _c = _cr.scalar_one_or_none()
                    if _c:
                        _c.ai_enabled = False
                        _c.taken_over_at = datetime.utcnow()
                        _c.taken_over_note = f"Auto-escalated: {reason}"
                        await db.commit()
                except Exception as exc:
                    logger.error("Failed to set human takeover: %s", exc)

                empathy_reply = _EMPATHY_REPLIES.get(
                    reason,
                    "Our team will assist you shortly.",
                )
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", empathy_reply)
                try:
                    await whatsapp_service.send_text_message(sender_phone, empathy_reply)
                except Exception as exc:
                    logger.error("Failed to send empathy reply: %s", exc)
                return {"status": "ok"}
    except Exception as exc:
        logger.error("Escalation check error: %s", exc)

    transcribed_text: str | None = None
    original_type: str | None = None

    try:
        if message.type == "image" and message.image is not None:
            logger.info("Image message from %s, media_id=%s", sender_phone, message.image.id)
            image_bytes = await vision_service.download_whatsapp_media(message.image.id)
            # Image messages carry no product code in user_text ("[image]"), so
            # the keyword-search catalogue_context is usually empty. Pass the
            # FULL catalogue instead — Learning 3: a customer asking "is this
            # available?" about a photo should get a matched OR similar product
            # suggestion, never a flat "no product code found" dead end.
            image_catalogue_context = catalogue_context
            if not image_catalogue_context and client:
                try:
                    all_products = await catalogue_service.list_products(db, client.id)
                    image_catalogue_context = catalogue_service.format_catalogue_context(all_products)
                except Exception:
                    image_catalogue_context = ""
            ai_reply = await vision_service.analyze_product_image(
                image_bytes,
                image_catalogue_context or "",
            )
            original_type = "image"
        elif message.type == "audio" and message.audio is not None:
            logger.info("Audio message from %s, media_id=%s", sender_phone, message.audio.id)
            # Send acknowledgement before transcription so customer isn't left waiting
            try:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text="🎤 Voice note suna. Ek second...",
                )
            except Exception as exc:
                logger.warning("Ack send failed for audio: %s", exc)
            audio_bytes = await vision_service.download_whatsapp_media(message.audio.id)
            mime = message.audio.mime_type or "audio/ogg"
            ext = mime.split("/")[-1].split(";")[0] or "ogg"
            transcribed_text = await voice_service.transcribe_voice_note(audio_bytes, f"audio.{ext}")
            effective_text = transcribed_text if transcribed_text else user_text
            ai_reply = await gemini_service.generate_reply(
                effective_text,
                history=history_dicts,
                system_prompt=system_prompt,
                catalogue_context=catalogue_context,
                language=language,
            )
            original_type = "audio"
        else:
            ai_reply = await gemini_service.generate_reply(
                user_text,
                history=history_dicts,
                system_prompt=system_prompt,
                catalogue_context=catalogue_context,
                language=language,
            )
    except Exception as exc:
        logger.error("AI processing error: %s", exc)
        return {"status": "ok"}

    # Issue 4 fix: override payment and confirmation messages with language-correct
    # templates so AI-generated Hindi never bleeds into an English conversation.
    # conv.current_stage is the OLD stage; stage is the NEW (just-detected) stage.
    from app.services.language_templates import get_template as _get_tpl
    _tpl_lang = getattr(conv, "last_customer_language", None) or language or "english"
    _tpl_order_total: float = 0
    if pinned_product and getattr(conv, "pending_order_quantity", None):
        _tpl_order_total = (
            (getattr(pinned_product, "price", 0) or 0)
            * (conv.pending_order_quantity or 0)
        )

    if stage == "payment" and conv.current_stage != "payment":
        # First turn entering payment stage — send template UPI message directly.
        _upi_val = getattr(client, "upi_id", None) if client else None
        if _upi_val and _tpl_order_total > 0:
            _amt_str = f"₹{int(_tpl_order_total):,}"
            _tpl_msg = _get_tpl(_tpl_lang, "ask_payment", amount=_amt_str, upi_id=_upi_val)
            if _tpl_msg:
                ai_reply = _tpl_msg

    elif stage == "completed" and conv.current_stage == "payment":
        # Payment just confirmed — template order confirmation in correct language.
        _qty_val = getattr(conv, "pending_order_quantity", 1) or 1
        _prod_name = (
            getattr(pinned_product, "name", None) if pinned_product else None
        ) or getattr(conv, "pending_product_sku", "product") or "product"
        _amt_str = f"{int(_tpl_order_total):,}" if _tpl_order_total > 0 else "—"
        _tpl_msg = _get_tpl(
            _tpl_lang, "order_confirmed",
            qty=_qty_val, product=_prod_name, total=_amt_str,
        )
        if _tpl_msg:
            ai_reply = _tpl_msg

    saved_user_content = transcribed_text if transcribed_text else user_text
    try:
        await conversation_service.save_message(db, conv.id, "user", saved_user_content, original_type=original_type, wamid=wamid)
        if transcribed_text:
            ai_reply = f"_{transcribed_text}_\n\n{ai_reply}"
        await conversation_service.save_message(db, conv.id, "assistant", ai_reply)
    except Exception as exc:
        logger.error("DB error saving messages for conv %s: %s", conv.id, exc)
        if transcribed_text:
            ai_reply = f"_{transcribed_text}_\n\n{ai_reply}"

    # Persist detected stage to conversation row
    try:
        await conversation_service.update_stage(db, conv.id, stage)
    except Exception as exc:
        logger.error("Stage update error: %s", exc)

    # Mark summary_shown when all order fields are collected and the AI just
    # replied in order_collection stage — the summary was sent in this turn.
    if (
        stage == "order_collection"
        and not getattr(conv, "summary_shown", False)
        and getattr(conv, "pending_order_quantity", None)
        and getattr(conv, "customer_name", None)
        and getattr(conv, "delivery_address", None)
        and "Order Summary" in ai_reply
    ):
        try:
            await conversation_service.update_order_field(db, conv.id, "summary_shown", True)
            conv.summary_shown = True
        except Exception as exc:
            logger.error("summary_shown update error: %s", exc)

    # Persist language — only when the message was not an ambiguous fallback
    # so that short replies don't accidentally overwrite the real language.
    if not _lang_svc.is_ambiguous(user_text):
        try:
            await conversation_service.update_language(db, conv.id, language)
        except Exception as exc:
            logger.error("Language update error: %s", exc)
        if customer_profile is not None:
            try:
                customer_profile.preferred_language = language
                await db.commit()
            except Exception as exc:
                logger.error("Customer language update error: %s", exc)

    try:
        await _record_usage(db, client)
    except Exception as exc:
        logger.error("Usage tracking error: %s", exc)

    all_messages = history_dicts + [
        {"role": "user", "content": user_text},
        {"role": "model", "content": ai_reply},
    ]
    try:
        await lead_service.tag_lead(db, sender_phone, conv.id, all_messages)
    except Exception as exc:
        logger.error("Lead tagging error: %s", exc)

    # Auto-create order when the conversation reaches 'completed' stage and all
    # required fields have been collected (name, address, quantity).
    if (
        stage == "completed"
        and client
        and getattr(conv, "customer_name", None)
        and getattr(conv, "delivery_address", None)
        and getattr(conv, "pending_order_quantity", None)
    ):
        # Check no order already exists for this conversation to avoid duplicates
        from app.models.order import Order as OrderModel
        existing_check = await db.execute(
            select(OrderModel).where(OrderModel.conversation_id == conv.id).limit(1)
        )
        if existing_check.scalar_one_or_none() is None:
            try:
                # Find the product discussed in this conversation.
                # Priority: pinned SKU from conversation > SKU in current message > keyword search.
                product = None
                pinned_sku = getattr(conv, "pending_product_sku", None)
                if pinned_sku:
                    product = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
                if not product:
                    sku_products = await _find_sku_matched_products(db, client, user_text)
                    if sku_products:
                        product = sku_products[0]
                if not product:
                    # Fall back to keyword search over full conversation history
                    all_prods = await catalogue_service.list_products(db, client.id)
                    all_msgs = history_dicts + [{"role": "user", "content": user_text}]
                    full_text = " ".join(m["content"] for m in all_msgs)
                    relevant = catalogue_service.search_products(all_prods, full_text)
                    if relevant:
                        product = relevant[0]

                # Resolve payment method: honour customer's explicit choice if
                # captured; if COD is disabled on this client default to UPI.
                accepts_cod = getattr(client, "accepts_cod", False)
                conv_payment = getattr(conv, "payment_method", None)
                if conv_payment == "COD" and not accepts_cod:
                    # Customer said COD but this seller doesn't accept it;
                    # override silently — agent already explained UPI-only.
                    conv_payment = "UPI"
                resolved_payment = conv_payment or ("COD" if accepts_cod else "UPI")

                created_order = await order_service.create_order(
                    db=db,
                    client_id=client.id,
                    conversation_id=conv.id,
                    customer_name=conv.customer_name,
                    customer_phone=sender_phone,
                    delivery_address=conv.delivery_address,
                    product_name=product.name if product else "Unknown",
                    product_sku=product.sku if product else None,
                    quantity=conv.pending_order_quantity,
                    unit_price=product.price if product else 0.0,
                    payment_method=resolved_payment,
                    product_id=product.id if product else None,
                )
                logger.info("Auto-created order %s from conversation %s", created_order.order_number, conv.id)

                # For UPI orders set status to payment_pending and send QR
                if resolved_payment == "UPI":
                    try:
                        from app.models.order import Order as _Order
                        result = await db.execute(
                            select(_Order).where(_Order.id == created_order.id)
                        )
                        _o = result.scalar_one_or_none()
                        if _o:
                            _o.status = "payment_pending"
                            await db.commit()
                        await _send_upi_qr(
                            sender_phone=sender_phone,
                            amount_inr=created_order.total_amount,
                            order_number=created_order.order_number,
                            client=client,
                            pid=webhook_phone_number_id or getattr(client, "whatsapp_phone_number_id", None),
                        )
                    except Exception as exc:
                        logger.warning("UPI QR dispatch failed for order %s: %s", created_order.order_number, exc)

                # Update customer profile stats
                try:
                    await customer_service.record_order(
                        db,
                        client_id=client.id,
                        phone=sender_phone,
                        order_total=created_order.total_amount,
                        customer_name=conv.customer_name,
                        delivery_address=conv.delivery_address,
                        payment_method=created_order.payment_method,
                    )
                    await db.commit()
                except Exception as exc:
                    logger.warning("Customer order stats update failed: %s", exc)

                # Decrement stock and fire low-stock / out-of-stock alerts
                if product and not created_order.stock_deducted:
                    try:
                        await _deduct_stock_and_alert(
                            db=db,
                            order=created_order,
                            product=product,
                            client=client,
                        )
                    except Exception as exc:
                        logger.warning("Stock decrement failed: %s", exc)
            except Exception as exc:
                logger.error("Auto-create order failed for conversation %s: %s", conv.id, exc)

    # Choose plain text vs interactive buttons based on stage + reply content.
    # Only WhatsApp supports interactive messages; Instagram always gets text.
    is_whatsapp = getattr(conv, "channel", "whatsapp") == "whatsapp"
    _client_accepts_cod = getattr(client, "accepts_cod", False) if client else False
    button_type = _should_use_buttons(stage, ai_reply, _client_accepts_cod) if is_whatsapp else "text"

    # phone_number_id: prefer the one in the webhook metadata (most accurate),
    # fall back to the client's configured ID.
    pid = webhook_phone_number_id or (
        getattr(client, "whatsapp_phone_number_id", None) if client else None
    )

    try:
        if button_type == "payment_buttons" and pid:
            # Build button list based on whether this seller accepts COD
            payment_buttons = [{"id": "upi", "title": "💳 Pay via UPI"}]
            if _client_accepts_cod:
                payment_buttons.append({"id": "cod", "title": "Cash on Delivery"})

            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=payment_buttons,
                phone_number_id=pid,
            )
            if not sent:
                # Meta rejected the interactive message — fall back to text
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        elif button_type == "confirm_buttons" and pid:
            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=[
                    {"id": "confirm_yes", "title": "✅ Confirm Order"},
                    {"id": "confirm_no", "title": "❌ Cancel"},
                ],
                phone_number_id=pid,
            )
            if not sent:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        else:
            await whatsapp_service.send_text_message(
                to_phone_number=sender_phone,
                message_text=ai_reply,
            )
    except Exception as exc:
        logger.error("WhatsApp send error: %s", exc)

    return {"status": "ok"}
