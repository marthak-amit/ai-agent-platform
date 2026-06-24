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
import re
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.schemas.webhook import WhatsAppWebhookPayload
from app.services import catalogue_service, conversation_service, cost_log, customer_service, escalation_service, gemini_service, lead_service, order_service, usage_service, vision_service, voice_service, whatsapp_service
from app.services import conversation_flow, language_service as _lang_svc, order_state_machine
from app.services.delivery_service import get_delivery_time_str
from app.services.language_templates import format_price
from app.services.order_state_machine import RenderError
from app.services.order_pipeline import (
    _DEFAULT_OFF_TOPIC_THRESHOLD,
    _NAME_MATCH_CONFIDENCE_FLOOR,
    _NAME_MATCH_SWITCH_MIN_SCORE,
    _SLOT_ATTEMPT_ESCALATE,
    _build_availability_answer,
    _build_order_aside_answer,
    _build_slot_question,
    _check_and_reset_llm_budget,
    _decode_btn,
    _detect_change_address_intent,
    _encode_btn,
    _is_availability_question,
    _is_order_aside_question,
    _is_simple_ack,
    _log_route,
    _rotate_nonce,
    run_blocklist_guard,
    run_cancel_in_payment_guard,
    run_button_nonce_guard,
    run_duplicate_confirm_tap_guard,
    run_duplicate_payment_word_guard,
    run_hard_llm_cap_guard,
    run_human_takeover_guard,
    run_stale_paid_guard,
    run_sku_and_name_pinning,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# NOTE: pure helper functions (nonce encode/decode, slot-question builder,
# aside-question/availability/ack detection, LLM budget check, route logging,
# etc.) moved to app/services/order_pipeline.py (SLICE 1 of the webhook.py
# strangler-fig refactor). Imported back above for existing call sites.

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
) -> tuple[str | None, list]:
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
        Tuple of (formatted catalogue string or None, list of Product instances).
        The product list is the canonical set for the SKU/price hallucination guard.
    """
    if client is None:
        return None, []

    # ── Pinned SKU (from a previous turn) ────────────────────────────────────
    pinned_sku = getattr(conv, "pending_product_sku", None) if conv else None
    if pinned_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
        if pinned:
            return catalogue_service.format_catalogue_context([pinned], for_display=True), [pinned]

    # ── SKU-first lookup ──────────────────────────────────────────────────────
    skus = catalogue_service.extract_skus_from_text(user_text)
    if skus:
        sku_products = []
        for sku in skus:
            p = await catalogue_service.find_product_by_sku(db, client.id, sku)
            if p:
                sku_products.append(p)
        if sku_products:
            return catalogue_service.format_catalogue_context(sku_products, for_display=True), sku_products

    # ── Keyword fallback ──────────────────────────────────────────────────────
    products = await catalogue_service.list_products(db, client.id)
    relevant = catalogue_service.search_products(products, user_text)
    if not relevant:
        return None, []
    return catalogue_service.format_catalogue_context(relevant, for_display=True), relevant


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
        # Exact-SKU fallback: short codes like "A001" (1 letter + 3 digits) don't
        # match the standard regex ({2,4} letters + {4,6} digits). If the entire
        # message is a bare alphanumeric token starting with a letter, try a direct
        # DB lookup so these short SKUs are pinned the same way as longer ones.
        # Require at least one digit — otherwise a plain word like "pink" or "red"
        # (a colour/attribute reply, not a SKU) can prefix-match a real SKU such as
        # "PINK_0001" via find_product_by_sku's startswith() fallback and silently
        # re-pin to the wrong product.
        candidate = user_text.strip().upper()
        if (
            candidate
            and candidate[0].isalpha()
            and candidate.isalnum()
            and any(c.isdigit() for c in candidate)
        ):
            p = await catalogue_service.find_product_by_sku(db, client.id, candidate)
            if p:
                return [p]
        return []
    matched = []
    for sku in skus:
        p = await catalogue_service.find_product_by_sku(db, client.id, sku)
        if p:
            matched.append(p)
    return matched


def _should_use_buttons(
    stage: str,
    ai_reply: str,
    accepts_cod: bool,
    next_slot: str | None = None,
) -> str:
    """
    Decide which interactive message type (if any) to use for an AI reply.

    Returns one of:
      "payment_buttons"  — show UPI (+ COD if client accepts it)
      "confirm_buttons"  — show Confirm Order / Cancel
      "text"             — plain text (default)

    Rules:
    - confirm_buttons fires FIRST in awaiting_final_confirmation when next_slot is None.
      Must take priority over payment_buttons because order summaries always contain
      "UPI"/"COD"/"payment" as the selected payment method — without this guard the
      summary would be misclassified as a payment-method prompt.
    - payment_buttons fires ONLY when next_slot is None or "payment_method"
      (all prior slots filled). Never when earlier slots are still missing.
    - Everything else (including completed, payment, greeting, etc.) is plain text.
    """
    lower = ai_reply.lower()

    # Confirm/Cancel buttons — PRIORITY CHECK: must come before payment_buttons.
    # Order summaries always contain "UPI"/"payment"/"COD" (the chosen payment method),
    # so the payment_buttons check would fire first and wrongly override confirm_buttons.
    if stage == "awaiting_final_confirmation" and next_slot is None:
        return "confirm_buttons"

    # Payment buttons — only when we are actually at the payment step
    if stage in ("order_collection", "awaiting_final_confirmation"):
        if next_slot in (None, "payment_method"):
            if any(kw in lower for kw in ("upi", "cod", "cash on delivery", "payment", "bhugtan", "payment mode")):
                return "payment_buttons"

    return "text"


async def _render_order_reply(
    action: str,
    conv,
    db,
    client,
    next_slot: str | None,
    variant_info: dict,
    customer_profile,
    available_stock: int | None,
    declined_saved_address: bool,
    lang: str,
    is_first_slot: bool = False,
    oos_product_name: str | None = None,
    attempt_count: int = 0,
) -> str:
    """
    Single render function: (action, db) → customer-facing text.

    All required facts (product name, price, stock, UPI ID, order data) are
    read from the database at render time — never from stale in-memory values.
    Raises RenderError when any required field is absent; the caller must not
    send a reply in that case.

    Args:
        action:                 Action key from order_state_machine.resolve_transition().
        conv:                   Conversation ORM instance (slots already written for this turn).
        db:                     Async DB session for fresh fact reads.
        client:                 Client ORM instance.
        next_slot:              Remaining unfilled slot (or None when all filled).
        variant_info:           Variant metadata dict (already-fetched this turn).
        customer_profile:       Optional CustomerProfile ORM (for saved address).
        available_stock:        Current stock for the pinned product/variant.
        declined_saved_address: True when customer just declined the saved address.
        lang:                   Customer language key.
        is_first_slot:          True on the very first slot question of an order.
        oos_product_name:       Product name for the oos_block action.

    Returns:
        Rendered template string ready to send.

    Raises:
        RenderError: A required DB fact is missing; caller must not send the reply.
    """
    from app.services.language_templates import get_template as _gt
    from app.services import catalogue_service as _cs

    # ── Fetch fresh product facts from DB ────────────────────────────────────
    _prod = None
    _prod_name = "product"
    _prod_price: float = 0
    _prod_sku = ""
    _pinned_sku = getattr(conv, "pending_product_sku", None)
    if _pinned_sku and client:
        _prod = await _cs.find_product_by_sku(db, client.id, _pinned_sku)
        if _prod is None and action not in ("cancel", "already_confirmed", "oos_block"):
            raise RenderError(
                f"Pinned product {_pinned_sku!r} not found in DB "
                f"(conv={conv.id}, action={action!r})"
            )
    if _prod:
        _prod_name = getattr(_prod, "name", None) or _pinned_sku or "product"
        _prod_price = getattr(_prod, "price", 0) or 0
        _prod_sku = getattr(_prod, "sku", "") or ""

    _qty = getattr(conv, "pending_order_quantity", None) or 0
    _total = int(_qty * _prod_price) if (_qty and _prod_price) else 0

    # Build variant string from slot values
    _v_parts = [
        p for p in [
            getattr(conv, "selected_color", None),
            getattr(conv, "selected_size", None),
            getattr(conv, "selected_material", None),
        ] if p
    ]
    _variant_str = ", ".join(_v_parts)
    _variant_part = (_variant_str + " ") if _variant_str else ""

    _accepts_cod = getattr(client, "accepts_cod", False) if client else False
    _sq_kwargs = dict(
        conv=conv,
        variant_info=variant_info,
        lang=lang,
        customer_profile=customer_profile,
        accepts_cod=_accepts_cod,
        available_stock=available_stock,
        product_name=_prod_name,
        declined_saved_address=declined_saved_address,
        attempt_count=attempt_count,
    )

    # ── Dispatch by action ────────────────────────────────────────────────────

    if action == "ask_slot":
        reply = _build_slot_question(next_slot, **_sq_kwargs)
        if is_first_slot and _prod:
            _pin_price = int(_prod_price)
            reply = f"{_prod_name} [{_prod_sku}] — ₹{_pin_price:,}\n{reply}"
        return reply

    if action == "cancel":
        return _gt(lang, "cancel_ack")

    if action == "discount_reask":
        _dsc = _gt(lang, "discount_policy")
        if next_slot:
            return f"{_dsc}\n\n{_build_slot_question(next_slot, **_sq_kwargs)}"
        return _dsc

    if action == "offtopic_reask":
        _sq = _build_slot_question(next_slot, **_sq_kwargs) if next_slot else ""
        return _gt(lang, "off_topic_midorder", slot_question=_sq)

    if action == "oos_block":
        return _gt(lang, "out_of_stock_block", product=oos_product_name or _prod_name)

    if action == "show_summary":
        # Hard invariant: all required fields must be present at render time.
        _name = getattr(conv, "customer_name", None)
        _addr = getattr(conv, "delivery_address", None)
        _pay  = getattr(conv, "payment_method", None)
        if not all([_name, _addr, _pay, _qty]):
            raise RenderError(
                f"show_summary: missing required field — "
                f"name={_name!r} addr={_addr!r} pay={_pay!r} qty={_qty!r} (conv={conv.id})"
            )
        _summary_delivery = get_delivery_time_str(_prod, client) or "3–7 business days"
        _total_fmt = format_price(_total)
        # Cross-sell: most-recently-browsed different SKU
        _cs_name: str | None = None
        _cs_price_fmt: str | None = None
        if not getattr(conv, "summary_shown", False) and client:
            import json as _json_cs
            try:
                _browsed = _json_cs.loads(getattr(conv, "browsed_skus", None) or "[]")
            except Exception:
                _browsed = []
            _others = [s for s in _browsed if s != _pinned_sku]
            if _others:
                try:
                    _cs_p = await _cs.find_product_by_sku(db, client.id, _others[-1])
                    if _cs_p and getattr(_cs_p, "name", None):
                        _cs_name = _cs_p.name
                        _cs_price_fmt = format_price(getattr(_cs_p, "price", 0) or 0)
                except Exception as exc:
                    import logging as _log
                    _log.getLogger(__name__).warning("cross-sell lookup failed: %s", exc)
        if _cs_name and _cs_price_fmt:
            _tpl = "order_summary_variant_crosssell" if _variant_str else "order_summary_crosssell"
            _kw: dict = dict(product=_prod_name, qty=_qty, total=_total_fmt,
                             name=_name, address=_addr, payment=_pay,
                             delivery_time=_summary_delivery,
                             cs_name=_cs_name, cs_price=_cs_price_fmt)
        else:
            _tpl = "order_summary_variant" if _variant_str else "order_summary"
            _kw = dict(product=_prod_name, qty=_qty, total=_total_fmt,
                       name=_name, address=_addr, payment=_pay,
                       delivery_time=_summary_delivery)
        if _variant_str:
            _kw["variant"] = _variant_str
        return _gt(lang, _tpl, **_kw)

    if action == "reask_confirm":
        return _gt(lang, "already_confirmed")

    if action in ("show_payment", "reask_payment"):
        _upi = getattr(client, "upi_id", None) if client else None
        if not _upi:
            raise RenderError(
                f"UPI ID missing for client {getattr(client, 'id', '?')} "
                f"(action={action!r}, conv={conv.id})"
            )
        _instr = getattr(client, "payment_instructions", None) if client else None
        _instr_part = f"\n{_instr}" if _instr else ""
        return (
            f"Please pay {format_price(_total)} to complete your order:\n"
            f"UPI ID: {_upi}\n"
            f"(GPay / PhonePe / Paytm){_instr_part}\n"
            f"Reply 'paid' once done. ✅"
        )

    if action in ("confirm_paid_upi", "confirm_paid_cod"):
        _name = getattr(conv, "customer_name", None) or ""
        _addr = getattr(conv, "delivery_address", None) or ""
        _delivery_time_r = get_delivery_time_str(_prod, client) or "3–7 business days"
        _cat_slug_r = getattr(client, "catalogue_slug", None) if client else None
        from app.config import get_settings as _gs_r
        _settings_r = _gs_r()
        _cat_url_r = (
            f"{_settings_r.catalogue_base_url}/{_cat_slug_r}"
            if _cat_slug_r else None
        )
        _catalogue_line = f"🛍️ Browse more: {_cat_url_r}" if _cat_url_r else ""
        _total_fmt = format_price(_total)
        if action == "confirm_paid_cod":
            return _gt(lang, "order_confirmed_cod",
                       product=_prod_name, variant_part=_variant_part,
                       qty=_qty or 1, total=_total_fmt,
                       delivery_time=_delivery_time_r,
                       catalogue_line=_catalogue_line)
        return _gt(lang, "order_confirmed_paid",
                   product=_prod_name, variant_part=_variant_part,
                   qty=_qty or 1, total=_total_fmt,
                   delivery_time=_delivery_time_r,
                   catalogue_line=_catalogue_line)

    if action == "already_confirmed":
        return _gt(lang, "already_confirmed")

    raise RenderError(f"Unknown action {action!r} for conv={conv.id}")


async def _send_upi_qr(
    sender_phone: str,
    amount_inr: float,
    order_number: str,
    client,
    pid: str | None,
) -> None:
    """
    Send payment instructions to the customer via WhatsApp.

    Priority:
    1. Razorpay QR code (per-client keys, falls back to global env vars).
    2. Plain UPI ID text with optional upi_display_name and payment_instructions.

    All errors are swallowed — payment collection must never crash the order flow.
    """
    _upi_id = getattr(client, "upi_id", None) if client else None
    _upi_display_name = getattr(client, "upi_display_name", None) if client else None
    _payment_instructions = getattr(client, "payment_instructions", None) if client else None
    _rz_key_id = getattr(client, "razorpay_key_id", None) if client else None
    _rz_key_secret = getattr(client, "razorpay_key_secret", None) if client else None

    from app.config import get_settings as _gs
    settings = _gs()

    # Prefer per-client keys; fall back to global env vars
    effective_key_id = _rz_key_id or settings.razorpay_key_id
    effective_key_secret = _rz_key_secret or settings.razorpay_key_secret
    razorpay_ready = bool(effective_key_id and effective_key_secret)

    if razorpay_ready:
        try:
            from app.services import razorpay_service

            qr_data = await razorpay_service.create_qr_code(
                amount=int(amount_inr * 100),  # paise
                description=f"Order {order_number}",
                phone_number=sender_phone,
                key_id=effective_key_id,
                key_secret=effective_key_secret,
            )
            image_url = qr_data.get("image_url") or qr_data.get("short_url")
            if image_url:
                caption = (
                    f"Scan this QR to pay {format_price(amount_inr)} for order {order_number}.\n"
                    f"Order will be dispatched after payment is confirmed. 🙏"
                )
                if _payment_instructions:
                    caption += f"\n\n{_payment_instructions}"
                await whatsapp_service.send_image_message(
                    to_phone_number=sender_phone,
                    image_url=image_url,
                    caption=caption,
                )
                return
        except Exception as exc:
            logger.warning("Razorpay QR generation failed, falling back to UPI text: %s", exc)

    # Fallback: send plain UPI ID text
    if _upi_id:
        name_part = f" ({_upi_display_name})" if _upi_display_name else ""
        instructions_part = f"\n\n{_payment_instructions}" if _payment_instructions else ""
        await whatsapp_service.send_text_message(
            to_phone_number=sender_phone,
            message_text=(
                f"Please pay {format_price(amount_inr)} via UPI to complete your order {order_number}.\n"
                f"UPI ID: {_upi_id}{name_part}{instructions_part}\n\n"
                f"Reply PAID when done. ✅"
            ),
        )
    else:
        logger.warning(
            "No Razorpay config and no UPI ID for client %s — cannot send payment request.",
            getattr(client, "id", "?"),
        )


async def _send_bank_transfer_details(
    sender_phone: str,
    amount_inr: float,
    order_number: str,
    client,
) -> None:
    """Send bank transfer payment details to customer via WhatsApp."""
    _account_name = getattr(client, "bank_account_name", None) if client else None
    _account_number = getattr(client, "bank_account_number", None) if client else None
    _ifsc = getattr(client, "bank_ifsc", None) if client else None
    _payment_instructions = getattr(client, "payment_instructions", None) if client else None

    if not (_account_name and _account_number and _ifsc):
        logger.warning("Bank transfer details incomplete for client %s", getattr(client, "id", "?"))
        return

    instructions_part = f"\n\n{_payment_instructions}" if _payment_instructions else ""
    await whatsapp_service.send_text_message(
        to_phone_number=sender_phone,
        message_text=(
            f"Please transfer {format_price(amount_inr)} to complete your order {order_number}.\n"
            f"Account Name: {_account_name}\n"
            f"Account Number: {_account_number}\n"
            f"IFSC: {_ifsc}{instructions_part}\n\n"
            f"Reply PAID when done. ✅"
        ),
    )


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
                await whatsapp_service.send_text_message(client.phone, alert)
            elif stock_remaining <= product.low_stock_alert:
                alert = (
                    f"⚠️ Low stock alert!\n"
                    f"{product.name} — only {stock_remaining} pieces remaining."
                )
                await whatsapp_service.send_text_message(client.phone, alert)
    except Exception as exc:
        logger.warning("Stock alert notification failed: %s", exc)


async def _reset_order_slots_after_completion(
    db, conv_id: int, conv
) -> None:
    """Reset order-cycle slots once an order reaches a terminal state (paid/completed).

    Clears all product-specific slots so the conversation is ready for a fresh
    order cycle without polluting it with the previous order's data.
    Customer name and delivery address are intentionally preserved — returning
    customers should not have to re-type them.

    BUG 1 FIX: called immediately after mark_order_paid() for both COD and UPI
    paths so the very next message the customer sends starts from a clean state.
    """
    _reset_fields = [
        ("pending_product_sku", None),
        ("selected_color", None),
        ("selected_size", None),
        ("selected_material", None),
        ("pending_order_quantity", None),
        ("payment_method", None),
        ("summary_shown", False),
        # Cleared here too — a completed order's product card must never be
        # repinned into a later, unrelated session via a stale bare "yes".
        ("last_shown_sku", None),
        ("pending_choice_skus", None),
    ]
    for _field, _value in _reset_fields:
        try:
            await conversation_service.update_order_field(db, conv_id, _field, _value)
            setattr(conv, _field, _value)
        except Exception as exc:
            logger.error("Completion reset (%s) for conv=%s: %s", _field, conv_id, exc)
    try:
        await conversation_service.update_stage(db, conv_id, "greeting")
        conv.current_stage = "greeting"
    except Exception as exc:
        logger.error("Completion stage reset for conv=%s: %s", conv_id, exc)
    logger.info(
        "Order completion reset: conv=%s — order slots cleared, stage→greeting "
        "(name/address preserved for next order cycle)",
        conv_id,
    )


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

    # Snapshot the stored stage BEFORE any mutations this turn.
    # Must be set here — at the top of receive_message, before any branching —
    # so every downstream code path (name-match guard, stage-lock, order-detect,
    # etc.) can reference the pre-update stage unconditionally.
    _stored_stage: str = conv.current_stage or "greeting"

    # P0-3: mirror pending_product_sku into last_shown_sku, which is NEVER
    # cleared by the post-order/cancel resets. This is what lets a bare "yes"
    # after order completion (when a new product card is shown) repin the
    # right SKU deterministically instead of falling through to the LLM.
    if conv.pending_product_sku and conv.pending_product_sku != conv.last_shown_sku:
        try:
            await conversation_service.update_order_field(db, conv.id, "last_shown_sku", conv.pending_product_sku)
            conv.last_shown_sku = conv.pending_product_sku
        except Exception as _lss_exc:
            logger.error("last_shown_sku sync error: %s", _lss_exc)

    # ── Improvement 3: Per-phone/day LLM budget check ─────────────────────────
    _llm_calls_today, _llm_soft_cap, _llm_hard_cap = await _check_and_reset_llm_budget(db, conv, client)
    _llm_budget = (
        "hard" if _llm_calls_today >= _llm_hard_cap
        else "soft" if _llm_calls_today >= _llm_soft_cap
        else "ok"
    )
    _llm_called_this_turn = False  # flipped True whenever any LLM call fires this turn
    _llm_usage = None  # set to gemini_service.get_last_usage() after any generate_reply() call
    # Cost-fix tracking: a pick resolved from pending_choice_skus this turn means the
    # product is already known — the open_browsing 70B classify must not also run.
    _pick_just_resolved = False
    # Count of catalog name-matches found this turn (set below); used to gate the
    # ROUTE reason logged for the final LLM branch (open_browsing_no_match vs
    # open_browsing — see VERIFY c in the cost-fix task).
    _name_match_count = 0

    # ── SLICE 2: early guards (delegated to order_pipeline) ──────────────────
    # Each guard below returns a PipelineResult when it fires (caller sends
    # the text and returns) or None when it doesn't (caller continues).

    if _llm_budget == "hard":
        _guard_result = await run_hard_llm_cap_guard(
            db, conv, client, sender_phone, user_text, wamid, _llm_calls_today,
        )
        try:
            await whatsapp_service.send_text_message(sender_phone, _guard_result.text)
        except Exception:
            pass
        return {"status": "ok"}

    # ── BUG 2 FIX: Stale "I've Paid" tap after cancel ────────────────────────
    # WhatsApp buttons stay tappable forever.  If the customer taps the old
    # "I've Paid" button after the order was cancelled (stage=greeting, no
    # pending_payment order), do NOT run it through the AI browse path — that
    # produces confusing "Which item are you interested in?" replies.
    # Instead reply with a clear, friendly message and return early.
    # This guard runs for BOTH the interactive button (paid_done) and the typed
    # "paid" keyword (typed into chat at a non-payment stage).
    _stale_paid_result = await run_stale_paid_guard(
        db, conv, sender_phone, user_text, wamid, _stored_stage,
    )
    if _stale_paid_result is not None:
        try:
            await whatsapp_service.send_text_message(sender_phone, _stale_paid_result.text)
        except Exception as exc:
            logger.error("Stale-paid send error: %s", exc)
        return {"status": "ok"}

    # ── BUG 4 FIX: validate button nonce ─────────────────────────────────────
    # If a nonce-encoded interactive button was received, check it against the
    # stored nonce.  A mismatch means the button is stale (e.g. from scroll-
    # back, after cancel, or a double-tap whose first tap already rotated the
    # nonce) → reply "That option has expired" and stop.  No state change.
    # We rotate the nonce immediately on a valid tap so a second tap of the
    # same button always fails.
    _nonce_guard_result = await run_button_nonce_guard(
        db, conv, sender_phone, user_text, wamid, _stored_stage, _btn_nonce_parsed,
    )
    if _nonce_guard_result is not None:
        try:
            await whatsapp_service.send_text_message(sender_phone, _nonce_guard_result.text)
        except Exception:
            pass
        return {"status": "ok"}

    # ── Cancel-in-payment guard ───────────────────────────────────────────────
    # When customer is in the "payment" stage (UPI: order created as
    # pending_payment, waiting for paid confirmation) and taps the Cancel button,
    # neither the AFC CANCEL block (needs stored=awaiting_final_confirmation) nor
    # the CANCEL intent block (needs stage=order_collection) fires.  Handle it
    # here as an early return: cancel the pending_payment order and go to greeting.
    _cancel_in_payment_result = await run_cancel_in_payment_guard(
        db, conv, client, sender_phone, user_text, wamid, _stored_stage, _record_usage,
    )
    if _cancel_in_payment_result is not None:
        try:
            await whatsapp_service.send_text_message(sender_phone, _cancel_in_payment_result.text)
        except Exception as exc:
            logger.error("Payment-stage cancel send error: %s", exc)
        return {"status": "ok"}

    # Upsert customer profile — best-effort; never block the message flow.
    # Block listed customers get no AI reply.
    if await run_blocklist_guard(db, client, sender_phone):
        return {"status": "ok"}

    # Human takeover: save message silently, skip AI entirely.
    _takeover_result = await run_human_takeover_guard(db, conv, sender_phone, user_text, wamid)
    if _takeover_result is not None:
        return {"status": "ok"}

    # ── Duplicate "Confirm Order" tap guard ───────────────────────────────────
    # WhatsApp button messages remain tappable indefinitely after the chat moves on.
    # When the conversation is already in "completed" stage and the customer taps
    # "Confirm Order" (or any affirmative button that looks like a confirmation),
    # short-circuit with a friendly plain-text reply — no AI call, no escalation.
    _dup_confirm_result = await run_duplicate_confirm_tap_guard(
        db, conv, message.type, sender_phone, user_text, wamid,
    )
    if _dup_confirm_result is not None:
        try:
            await whatsapp_service.send_text_message(sender_phone, _dup_confirm_result.text)
        except Exception as exc:
            logger.error("Send error for duplicate confirm reply: %s", exc)
        return {"status": "ok"}

    # FIX E: Payment-confirmation words sent AFTER order is already completed
    # (e.g. customer taps "PAID" again, or sends "done"/"transferred") must
    # never trigger a fresh AI "Order confirmed!" reply or a second Order row.
    # Return a deterministic already-confirmed message and stop processing.
    _dup_payment_result = await run_duplicate_payment_word_guard(
        db, conv, message.type, sender_phone, user_text, wamid,
    )
    if _dup_payment_result is not None:
        try:
            await whatsapp_service.send_text_message(sender_phone, _dup_payment_result.text)
        except Exception as exc:
            logger.error("Send error for duplicate payment reply: %s", exc)
        return {"status": "ok"}

    settings = get_settings()
    delay = random.uniform(settings.min_reply_delay, settings.max_reply_delay)
    await asyncio.sleep(delay)

    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]
    # Cap history globally before any stage branching — prevents 413/429 on all Groq calls.
    history_dicts = history_dicts[-10:]
    catalogue_context, _canonical_browse_products = await _get_catalogue_context(db, client, user_text, conv=conv)

    # Deferred image queue: images are collected here during SKU/name-match
    # blocks and sent AFTER the main text reply so a send failure never
    # aborts the pin path or prevents the text from reaching the customer.
    # Entries: (image_url, caption)
    _pending_product_images: list[tuple[str, str]] = []

    # ── SLICE 3: SKU / product-name pinning + match (delegated to order_pipeline) ──
    _pin_outcome = await run_sku_and_name_pinning(
        db, conv, client, message, user_text, sender_phone, wamid, _stored_stage,
        _record_usage, catalogue_context, _canonical_browse_products,
        _pending_product_images, _find_sku_matched_products, _is_product_out_of_stock,
    )
    if _pin_outcome.early_result is not None:
        _pin_early = _pin_outcome.early_result
        if not _pin_early.skip_send and _pin_early.text:
            try:
                await whatsapp_service.send_text_message(sender_phone, _pin_early.text)
            except Exception as exc:
                if _pin_outcome.early_send_error_label:
                    logger.error(_pin_outcome.early_send_error_label, exc)
        return {"status": "ok"}
    pinned_product = _pin_outcome.pinned_product
    variant_info = _pin_outcome.variant_info
    catalogue_context = _pin_outcome.catalogue_context
    _canonical_browse_products = _pin_outcome.canonical_browse_products
    _pick_just_resolved = _pin_outcome.pick_just_resolved
    _p03_repinned = _pin_outcome.p03_repinned
    customer_profile = None

    # ── Sales pipeline: stage → focused prompt ───────────────────────────────
    # Pass previous language so ambiguous single-word replies ("yes", "COD")
    # inherit the customer's established language rather than resetting.
    previous_language = getattr(conv, "last_customer_language", None) or "english"
    language = _lang_svc.detect_language(user_text, previous_language=previous_language)

    # Detect conversation stage — pass stored stage so payment/completed are locked,
    # and pending_product_sku so Mode A → Mode B transition fires correctly.
    stage = conversation_flow.detect_stage(
        history_dicts,
        user_text,
        stored_stage=conv.current_stage,
        pending_product_sku=getattr(conv, "pending_product_sku", None),
    )

    # P0-3: the repin above means the customer just affirmed an actual product
    # card — force order_collection now rather than waiting for detect_stage's
    # "AI last offered order" heuristic, which won't fire since the order
    # offer was never re-sent after the reset.
    if _p03_repinned:
        stage = "order_collection"
        try:
            await conversation_service.update_stage(db, conv.id, "order_collection")
            conv.current_stage = "order_collection"
        except Exception as _p03_stage_exc:
            logger.error("P0-3 stage-force error: %s", _p03_stage_exc)

    # ── LLM buy-intent gate (Phase 1 — DOC A/B) ──────────────────────────────
    # Replaces keyword-based Mode A→B affirmation.  When the customer is in any
    # browsing stage with a product already pinned, ask the LLM whether the
    # message expresses intent to purchase.  If yes, enter order_collection
    # immediately — no keyword list required, any phrasing is caught.
    #
    # Conditions: text message only; pinned product; stage is still browsing;
    #             NOT already locked inside order_collection/payment/completed.
    # NOTE: _stored_stage is initialized at the top of receive_message (before
    # any branching) so it is always defined on every path.
    _BROWSING_STAGES_GATE = frozenset({
        "greeting", "product_inquiry", "qualification",
        "objection_handling", "offer_making",
    })
    # Affirmatives that unambiguously mean "yes, I want to order" with no LLM needed.
    _BROWSE_ORDER_AFFIRMATIONS: frozenset[str] = frozenset({
        "yes", "haan", "ha", "han", "ok", "okay", "sure", "bilkul",
        "yes please", "haan please", "order karo", "order", "chahiye",
        # "confirm" is now the canonical confirm_pay button translation (BUG 3 FIX —
        # replaces "1" which collided with valid quantity answers).
        "confirm", "1", "हाँ", "હા", "ہاں", "yep", "yup", "yeah",
    })
    if (
        message.type == "text"
        and stage in _BROWSING_STAGES_GATE
        and getattr(conv, "pending_product_sku", None)
        and pinned_product is not None
        and _stored_stage not in ("order_collection", "awaiting_final_confirmation", "payment", "completed")
    ):
        _pinned_name = getattr(pinned_product, "name", None) or conv.pending_product_sku
        _text_stripped = user_text.strip().lower()
        if _text_stripped in _BROWSE_ORDER_AFFIRMATIONS:
            # Deterministic fast-path: affirmation on a pinned product → order.
            stage = "order_collection"
            logger.info(
                "conv=%s deterministic affirmation %r → order_collection (product=%r) — no LLM call",
                conv.id, _text_stripped, _pinned_name,
            )
        else:
            try:
                _is_buy_intent = await conversation_flow.classify_buy_intent(user_text, _pinned_name, conversation_id=conv.id)
                _llm_called_this_turn = True
            except Exception as _bie:
                logger.warning("classify_buy_intent error (defaulting False): %s", _bie)
                _is_buy_intent = False
            if _is_buy_intent:
                stage = "order_collection"
                logger.info(
                    "conv=%s LLM buy-intent detected — overriding stage %r → order_collection "
                    "(product=%r text=%r)",
                    conv.id, _stored_stage, _pinned_name, user_text[:60],
                )

    # ── Stage-lock: prevent oscillation mid-slot-fill ─────────────────────────
    # The keyword classifier can't tell "7" (quantity answer) from a random
    # number, or "red" (color answer) from a browse query — it falls back to
    # "product_inquiry" for short/ambiguous replies. Override that whenever:
    #   • a product is pinned (pending_product_sku set), AND
    #   • the stored stage is already order_collection or awaiting_final_confirmation
    #   • AND the classifier didn't detect a terminal state (payment/completed)
    # The stored stage is the ground truth for "we are actively collecting slots".
    #
    # Exception: when stored=awaiting_final_confirmation and the classifier
    # returned "order_collection", that is a deliberate AFC → cancel/change
    # transition (detect_stage maps _CONFIRMATION_NO → "order_collection").
    # Locking it back to AFC would silently re-render the summary.
    _afc_cancel_transition = (
        _stored_stage == "awaiting_final_confirmation" and stage == "order_collection"
    )
    if (
        getattr(conv, "pending_product_sku", None)
        and _stored_stage in ("order_collection", "awaiting_final_confirmation")
        and stage not in ("completed", "payment", "awaiting_final_confirmation")
        and not _afc_cancel_transition
    ):
        stage = _stored_stage
        logger.debug(
            "Stage-lock: keyword classifier returned %r but stored=%r with pinned SKU — keeping %r",
            stage, _stored_stage, _stored_stage,
        )

    # ── Payment-keyword false-trigger guard ───────────────────────────────────
    # detect_stage returns "payment" for ANY message containing "upi"/"gpay"/
    # "paytm" etc. — even during casual browsing ("do you accept gpay?").
    # This guard blocks the jump when no order flow is active: either the stored
    # stage is a browsing stage, or there's no pinned product yet.
    if (
        stage == "payment"
        and _stored_stage in {"greeting", "product_inquiry", "qualification", "objection_handling", "offer_making"}
        and not getattr(conv, "pending_product_sku", None)
    ):
        stage = _stored_stage
        logger.debug(
            "Payment-keyword guard: reverted 'payment' → %r (no active order, stored=%r)",
            _stored_stage, _stored_stage,
        )

    # ── Phase 0 shadow router — fire-and-forget, zero behavior change ────────────
    # When USE_TOOL_ROUTER=False the router call runs in the background and logs
    # its proposed tool calls alongside what the keyword layer decided. No slot
    # writes, no stage changes — pure observation.
    _shadow_stage_snapshot = stage
    _shadow_next_slot_snapshot = conversation_flow.get_next_required_slot(conv, variant_info)
    _shadow_product_name = getattr(pinned_product, "name", None) or getattr(conv, "pending_product_sku", "") or ""

    async def _shadow_router_task() -> None:
        """Background shadow: call tool router and log disagreements."""
        from app.config import get_settings as _gs
        from app.services.tool_router import call_tool_router as _call_router
        try:
            _cfg = _gs()
            if _cfg.use_tool_router:
                return  # live mode — not a shadow task anymore
            _proposals = await _call_router(
                user_text=user_text,
                stage=_shadow_stage_snapshot,
                next_slot=_shadow_next_slot_snapshot,
                variant_info=variant_info,
                conversation_history=history_dicts,
                product_name=_shadow_product_name,
            )
            logger.info(
                "SHADOW conv=%s stage_kw=%r next_slot_kw=%r router_proposals=%s",
                conv.id,
                _shadow_stage_snapshot,
                _shadow_next_slot_snapshot,
                _proposals,
            )
        except Exception as _se:
            logger.debug("SHADOW router task error (non-fatal): %s", _se)

    if settings.shadow_router_enabled:
        asyncio.create_task(_shadow_router_task())

    # ── UPI-flow stage override ────────────────────────────────────────────────
    # When the customer confirms the order summary ("yes") and the payment method
    # is UPI, we must NOT jump to "completed" yet — the customer still has to
    # pay. Redirect to "payment" so the bot sends UPI instructions and waits
    # for a "paid" confirmation before marking the order confirmed.
    if (
        stage == "completed"
        and _stored_stage == "awaiting_final_confirmation"
        and getattr(conv, "payment_method", None) == "UPI"
    ):
        stage = "payment"
        logger.info(
            "UPI-flow override: awaiting_final_confirmation+yes → payment (pending_payment) conv=%s",
            conv.id,
        )

    # ── Parse pre-filled catalogue order messages ─────────────────────────────
    # When customer taps "Order" on the public catalogue a structured message
    # like "Product: Designer Lehenga\nSKU: LH10042\nColor: Red\nSize: XL" is
    # sent. Extract and persist those fields immediately so the agent skips the
    # questions that were already answered by the catalogue form.
    if client and stage in ("order_collection", "awaiting_final_confirmation"):
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

    # ── Compute available_stock for the pinned product (variant-aware) ─────────
    # Only run when all required variant attributes are known (i.e. we are at
    # the quantity slot).  Running earlier with a partial filter causes
    # MultipleResultsFound when a product has e.g. Black/S + Black/M + … rows.
    available_stock: int | None = None
    if pinned_product:
        if pinned_product.has_variants:
            sel_color    = getattr(conv, "selected_color",    None)
            sel_size     = getattr(conv, "selected_size",     None)
            sel_material = getattr(conv, "selected_material", None)
            _vi          = variant_info  # populated above
            _needs_color    = _vi.get("needs_color",    False)
            _needs_size     = _vi.get("needs_size",     False)
            _needs_material = _vi.get("needs_material", False)
            # All required attrs must be filled before we can identify a single variant.
            _all_filled = (
                (not _needs_color    or sel_color)    and
                (not _needs_size     or sel_size)     and
                (not _needs_material or sel_material)
            )
            if _all_filled and (sel_color or sel_size or sel_material):
                from app.models.product_variant import ProductVariant as _PV
                _vstmt = select(_PV).where(
                    _PV.product_id == pinned_product.id,
                    _PV.is_active == True,  # noqa: E712
                )
                if sel_color:
                    _vstmt = _vstmt.where(_PV.color == sel_color)
                if sel_size:
                    _vstmt = _vstmt.where(_PV.size == sel_size)
                if sel_material:
                    _vstmt = _vstmt.where(_PV.material == sel_material)
                _vr = await db.execute(_vstmt)
                _variant = _vr.scalars().first()
                available_stock = _variant.stock if _variant else 0
            # else: defer — not all attributes chosen yet; leave available_stock=None
        else:
            available_stock = pinned_product.stock

    # ── OOS combo gate: if all variant attrs selected but stock=0, clear last attr ──
    # Must run AFTER available_stock is computed (above) and BEFORE slot extraction.
    _combo_oos = False
    if (
        pinned_product
        and getattr(pinned_product, "has_variants", False)
        and available_stock is not None
        and available_stock <= 0
        and stage in ("order_collection",)
    ):
        # All required attrs are filled (else available_stock would be None).
        # Clear the most specific variant attr so customer can re-pick.
        _vi_co = variant_info
        _clear_combo_field = None
        if _vi_co.get("needs_material") and getattr(conv, "selected_material", None):
            _clear_combo_field = "selected_material"
        elif _vi_co.get("needs_size") and getattr(conv, "selected_size", None):
            _clear_combo_field = "selected_size"
        elif _vi_co.get("needs_color") and getattr(conv, "selected_color", None):
            _clear_combo_field = "selected_color"
        if _clear_combo_field:
            try:
                await conversation_service.update_order_field(db, conv.id, _clear_combo_field, None)
                setattr(conv, _clear_combo_field, None)
            except Exception as exc:
                logger.error("OOS combo clear (%s): %s", _clear_combo_field, exc)
            _combo_oos = True
            available_stock = None  # reset so quantity check doesn't block
            conv.__dict__["_combo_oos_cleared_field"] = _clear_combo_field  # Fix C
            logger.info(
                "OOS combo gate fired: conv=%s — cleared %s, re-asking that slot",
                conv.id, _clear_combo_field,
            )

    # Fix A: scope available_sizes to the already-selected color so only in-stock
    # sizes for that color are offered (and accepted) going forward.
    if (
        pinned_product
        and getattr(pinned_product, "has_variants", False)
        and variant_info.get("needs_size")
        and getattr(conv, "selected_color", None)
    ):
        try:
            _fa_opts = await catalogue_service.get_in_stock_options(
                db, pinned_product.id, color=conv.selected_color
            )
            if _fa_opts.get("sizes"):
                variant_info = {**variant_info, "available_sizes": _fa_opts["sizes"]}
        except Exception as _fa_exc:
            logger.warning("Fix A color-scope failed: %s", _fa_exc)

    # ── Customer profile — fetched here so slot extraction can use saved address ──
    # NOTE: a second (no-op) assignment exists below; it is kept for code-flow
    # clarity but will simply overwrite this reference with the same object.
    if client and customer_profile is None:
        try:
            customer_profile = await customer_service.get_customer(
                db, client_id=client.id, phone=sender_phone
            )
        except Exception:
            pass

    # ── Auto-fill name from profile (skip name slot for returning customers) ───
    # If the profile has a saved name and conv.customer_name is not set yet,
    # write it now so the slot machine skips the name question entirely.
    if (
        stage == "order_collection"
        and customer_profile is not None
        and getattr(customer_profile, "name", None)
        and not getattr(conv, "customer_name", None)
    ):
        try:
            await conversation_service.update_order_field(
                db, conv.id, "customer_name", customer_profile.name
            )
            conv.customer_name = customer_profile.name
            logger.info(
                "Auto-filled customer_name from profile: conv=%s name=%r",
                conv.id, customer_profile.name,
            )
        except Exception as exc:
            logger.error("Auto-fill name from profile error: %s", exc)

    # ── Slot-machine: detect quantity_invalid flag before building prompt ──────
    _quantity_invalid = False

    # ── awaiting_final_confirmation: handle "2 = add cross-sell" choice ────────
    # "2" only means "add cross-sell" when the summary that was just shown
    # actually included a cross-sell option (template uses "2️⃣ Add …").
    # In the plain summary (no cross-sell), "2" means CANCEL — handled below.
    _crosssell_sku_for_session: str | None = None  # populated below for use in summary render
    _afc_crosssell_switched = False  # True when "2" triggers a cross-sell product switch
    if _stored_stage == "awaiting_final_confirmation" and client:
        _cs_msg = user_text.strip().lower()
        if _cs_msg in ("2", "add"):
            # Gate: only attempt cross-sell if the last AI message contained the
            # cross-sell offer marker ("2️⃣ add").  Without this guard, any browsed
            # SKU causes "2" to switch products instead of cancelling.
            _last_ai_for_cs = next(
                (m.get("content", "") for m in reversed(history_dicts)
                 if m.get("role") in ("model", "assistant")),
                "",
            ) or ""
            _crosssell_shown_in_summary = "2️⃣ add" in _last_ai_for_cs.lower()

            if _crosssell_shown_in_summary:
                import json as _json_cs2
                _browsed_raw_cs2 = getattr(conv, "browsed_skus", None) or "[]"
                try:
                    _browsed_cs2 = _json_cs2.loads(_browsed_raw_cs2)
                except Exception:
                    _browsed_cs2 = []
                _current_sku_cs2 = getattr(conv, "pending_product_sku", None)
                _other_skus_cs2 = [s for s in _browsed_cs2 if s != _current_sku_cs2]
                if _other_skus_cs2:
                    _new_cs_sku = _other_skus_cs2[-1]
                    try:
                        _cs_new_product = await catalogue_service.find_product_by_sku(db, client.id, _new_cs_sku)
                        if _cs_new_product:
                            # Reset all order slots for the new (cross-sell) product
                            _cs_reset_fields = [
                                ("pending_product_sku", _new_cs_sku),
                                ("pending_order_quantity", None),
                                ("selected_color", None),
                                ("selected_size", None),
                                ("selected_material", None),
                                ("customer_name", None),
                                ("delivery_address", None),
                                ("payment_method", None),
                                ("summary_shown", False),
                            ]
                            for _csf, _csv in _cs_reset_fields:
                                try:
                                    await conversation_service.update_order_field(db, conv.id, _csf, _csv)
                                    setattr(conv, _csf, _csv)
                                except Exception as _cse:
                                    logger.error("Cross-sell slot reset (%s): %s", _csf, _cse)
                            pinned_product = _cs_new_product
                            variant_info = await catalogue_service.get_product_variant_info(db, _cs_new_product)
                            stage = "order_collection"
                            _afc_crosssell_switched = True
                            logger.info(
                                "conv=%s cross-sell choice '2' → switched to SKU=%s",
                                conv.id, _new_cs_sku,
                            )
                    except Exception as _cs2e:
                        logger.error("Cross-sell '2' pin error: %s", _cs2e)
            # If no cross-sell was shown, "2" falls through to the cancel block below.

    # ── awaiting_final_confirmation: handle negative confirmation slot resets ──
    if stage == "order_collection" and conv.current_stage == "awaiting_final_confirmation":
        # Customer said "no / change" — inspect their message to reset the
        # specific slot(s) they want to change, keeping the rest intact.
        _msg_lower = user_text.lower()
        _slots_to_reset: list[tuple[str, str]] = []
        if any(kw in _msg_lower for kw in ("color", "colour", "rang")):
            _slots_to_reset.append(("selected_color", None))
        if any(kw in _msg_lower for kw in ("size", "saiz")):
            _slots_to_reset.append(("selected_size", None))
        if any(kw in _msg_lower for kw in ("material", "kapda", "fabric")):
            _slots_to_reset.append(("selected_material", None))
        if any(kw in _msg_lower for kw in ("quantity", "kitne", "pieces", "amount")):
            _slots_to_reset.append(("pending_order_quantity", None))
        if any(kw in _msg_lower for kw in ("name", "naam")):
            _slots_to_reset.append(("customer_name", None))
        if any(kw in _msg_lower for kw in ("address", "pata")):
            _slots_to_reset.append(("delivery_address", None))
        if any(kw in _msg_lower for kw in ("payment", "pay", "bhugtan")):
            _slots_to_reset.append(("payment_method", None))
        # If customer said "no"/"cancel"/"2" without specifying what field to
        # change, treat as a full order cancel: reset everything, go to greeting,
        # and short-circuit with a deterministic cancel reply.
        # Exception: "2" was already handled above as a cross-sell switch — skip.
        if not _slots_to_reset and not _afc_crosssell_switched:
            # ── Cancel any pending_payment order for this conversation (BUG 1 FIX) ──
            # The order is created when customer taps Confirm (stage→payment). If they
            # then tap the old Cancel button, we must mark that row cancelled so it
            # never stays stuck as pending_payment forever.
            # Stock is NOT deducted until mark_order_paid(), so no stock restore needed.
            from app.models.order import Order as _CancelOrderModel
            try:
                _cancel_order_result = await db.execute(
                    select(_CancelOrderModel)
                    .where(
                        _CancelOrderModel.conversation_id == conv.id,
                        _CancelOrderModel.status == "pending_payment",
                    )
                    .order_by(_CancelOrderModel.created_at.desc())
                    .limit(1)
                )
                _order_to_cancel = _cancel_order_result.scalar_one_or_none()
                if _order_to_cancel:
                    _order_to_cancel.status = "cancelled"
                    await db.commit()
                    logger.info(
                        "Order %s → cancelled (conv=%s) [AFC CANCEL]",
                        _order_to_cancel.order_number, conv.id,
                    )
            except Exception as exc:
                logger.error("AFC cancel: order cancellation failed for conv=%s: %s", conv.id, exc)

            _full_cancel_fields = [
                ("pending_order_quantity", None), ("selected_color", None),
                ("selected_size", None), ("selected_material", None),
                ("customer_name", None), ("delivery_address", None),
                ("payment_method", None), ("summary_shown", False),
                ("pending_product_sku", None), ("interrupted_sku", None),
                ("last_shown_sku", None), ("pending_choice_skus", None),
            ]
            for _cf, _cv in _full_cancel_fields:
                try:
                    await conversation_service.update_order_field(db, conv.id, _cf, _cv)
                    setattr(conv, _cf, _cv)
                except Exception as exc:
                    logger.error("AFC full cancel reset (%s): %s", _cf, exc)
            try:
                await conversation_service.update_stage(db, conv.id, "greeting")
                stage = "greeting"
                conv.current_stage = "greeting"
            except Exception as exc:
                logger.error("AFC cancel stage reset: %s", exc)
            _afc_lang = getattr(conv, "last_customer_language", None) or "english"
            if _afc_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                _cancel_reply = "Order cancel kar diya gaya. ✅ Kya main kuch aur help kar sakta hoon?"
            elif _afc_lang in ("gujarati_roman", "gujarati_script"):
                _cancel_reply = "Order cancel thai gayu. ✅ Koi biju kaam hoy to kaho!"
            else:
                _cancel_reply = "Order cancelled. ✅ Anything else I can help you with?"
            logger.info("conv=%s AFC CANCEL — all slots reset, stage=greeting", conv.id)
            try:
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", _cancel_reply)
            except Exception as exc:
                logger.error("AFC cancel save error: %s", exc)
            try:
                await whatsapp_service.send_text_message(sender_phone, _cancel_reply)
            except Exception as exc:
                logger.error("AFC cancel send error: %s", exc)
            try:
                await _record_usage(db, client)
            except Exception:
                pass
            return {"status": "ok"}
        for _field, _val in _slots_to_reset:
            try:
                await conversation_service.update_order_field(db, conv.id, _field, _val)
                setattr(conv, _field, _val)
            except Exception as exc:
                logger.error("Slot reset error (%s): %s", _field, exc)

    # ── Slot-machine extraction during order_collection ────────────────────────
    # Intent pre-classification: before extracting a slot value, check whether
    # the customer is actually answering the question or doing something else
    # (switching product, cancelling, or asking an off-topic question).
    _intent_override: str | None = None  # used only for AI calls in non-order stages
    _classified_intent: str | None = None  # tracks the classification result across blocks
    _switch_resolved: bool = False  # set True when interrupted_sku was just resolved (continue OR switch)

    if stage == "order_collection":
        _next_slot_pre = conversation_flow.get_next_required_slot(conv, variant_info)

        # Improvement 1: detect slot change → auto-reset counter; read current attempts
        if getattr(conv, "slot_attempt_slot", None) != _next_slot_pre:
            conv.slot_attempt_count = 0
            conv.slot_attempt_slot = _next_slot_pre
            try:
                await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", 0)
                await conversation_service.update_order_field(db, conv.id, "slot_attempt_slot", _next_slot_pre)
            except Exception as _slc_exc:
                logger.error("slot_attempt auto-reset error: %s", _slc_exc)
        _current_slot_attempts = conv.slot_attempt_count or 0

        if stage == "order_collection" and _next_slot_pre is not None:
            # Improvement 1: hard cap — skip LLM entirely at ≥ ESCALATE attempts
            if _current_slot_attempts >= _SLOT_ATTEMPT_ESCALATE:
                _cap_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _cap_prod = getattr(pinned_product, "name", "the product") or "the product"
                _cap_sq = _build_slot_question(
                    _next_slot_pre, conv, variant_info, _cap_lang,
                    customer_profile=customer_profile,
                    accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                    available_stock=available_stock,
                    product_name=_cap_prod,
                    attempt_count=_current_slot_attempts,
                )
                if _cap_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                    _cap_reply = f"Hamara team aapki madad karega. 👋\n\n{_cap_sq}"
                elif _cap_lang in ("gujarati_roman", "gujarati_script"):
                    _cap_reply = f"Amari team tamne madad karse. 👋\n\n{_cap_sq}"
                else:
                    _cap_reply = f"Our team will assist you shortly. 👋\n\n{_cap_sq}"
                logger.warning(
                    "SLOT cap: conv=%s slot=%r attempts=%d — no LLM, escalating to human.",
                    conv.id, _next_slot_pre, _current_slot_attempts,
                )
                try:
                    from app.models.conversation import Conversation as _ConvCap
                    from sqlalchemy import select as _selCap
                    _cap_cr = await db.execute(_selCap(_ConvCap).where(_ConvCap.id == conv.id).limit(1))
                    _cap_cobj = _cap_cr.scalar_one_or_none()
                    if _cap_cobj:
                        _cap_cobj.ai_enabled = False
                        _cap_cobj.taken_over_at = datetime.utcnow()
                        _cap_cobj.taken_over_note = f"Slot cap: {_next_slot_pre} x{_current_slot_attempts}"
                        await db.commit()
                except Exception as _capesc:
                    logger.error("Slot-cap escalation error: %s", _capesc)
                try:
                    await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                    await conversation_service.save_message(db, conv.id, "assistant", _cap_reply)
                except Exception:
                    pass
                try:
                    await whatsapp_service.send_text_message(sender_phone, _cap_reply)
                except Exception:
                    pass
                try:
                    await _record_usage(db, client)
                except Exception:
                    pass
                return {"status": "ok"}

            # ── FIX 3: Change-address intent mid-order ────────────────────────
            _ca_detected, _ca_addr = _detect_change_address_intent(user_text)
            if _ca_detected and message.type == "text":
                from app.services.conversation_flow import is_valid_address as _is_valid_addr_fix3
                _ca_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _ca_prod_name = getattr(pinned_product, "name", "the product") or "the product"
                if _ca_addr and _is_valid_addr_fix3(_ca_addr):
                    try:
                        await conversation_service.update_order_field(db, conv.id, "delivery_address", _ca_addr)
                        conv.delivery_address = _ca_addr
                        logger.info("FIX3 change-address: conv=%s new_addr=%r", conv.id, _ca_addr)
                        logger.info("Address change captured conv=%s cleaned=%r", conv.id, _ca_addr)
                    except Exception as _cae:
                        logger.error("FIX3 address update error: %s", _cae)
                    # Fall through — re-compute next_slot and let normal reply-build handle it.
                else:
                    _ca_ask = "Sure — what's the new delivery address? Please send house/area, city and pincode."
                    # P1-5: clear the OLD address now so next_slot resolves back to
                    # delivery_address on the next turn — otherwise the slot machine
                    # sees the stale address as "already filled" and the customer's
                    # very next message (the new address) gets dropped.
                    try:
                        await conversation_service.update_order_field(db, conv.id, "delivery_address", None)
                        conv.delivery_address = None
                        await conversation_service.update_order_field(db, conv.id, "summary_shown", False)
                        conv.summary_shown = False
                    except Exception as _ca_clr_exc:
                        logger.error("FIX3 address clear error: %s", _ca_clr_exc)
                    logger.info("FIX3 change-address pure intent: conv=%s — asking for address", conv.id)
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _ca_ask)
                    except Exception:
                        pass
                    try:
                        await whatsapp_service.send_text_message(sender_phone, _ca_ask)
                    except Exception:
                        pass
                    try:
                        await _record_usage(db, client)
                    except Exception:
                        pass
                    return {"status": "ok"}

            # ── FIX 2 / FIX 4: Aside-question — answer + re-ask slot ─────────
            # Intercept side-questions (delivery charges, price of another product,
            # etc.) BEFORE intent classification so they are never treated as slot
            # answers and the current slot is re-asked after the one-line answer.
            if message.type == "text" and _is_order_aside_question(user_text):
                _aq_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _aq_prod_name = getattr(pinned_product, "name", "the product") or "the product"
                _aq_dt = get_delivery_time_str(pinned_product, client) or "3–7 business days"
                _aq_answer = ""

                # BUG 1 FIX: availability questions ("is orange available?") are
                # resolved against the CATALOG / current product's variants, never
                # the KB — KB keyword overlap on a word like "available" was
                # returning unrelated FAQs (e.g. a dispatch/tracking entry).
                if _is_availability_question(user_text):
                    _aq_answer = _build_availability_answer(
                        user_text, pinned_product, variant_info, available_stock,
                    )
                    if _aq_answer:
                        logger.info(
                            "FIX2 availability question: conv=%s answered from catalog/variants, skipping KB",
                            conv.id,
                        )

                # FIX 4: question names a DIFFERENT product — look it up in catalogue
                # FIRST. A confident named-product match is more specific than the
                # generic pinned-product answer below, so it must take priority —
                # otherwise _build_order_aside_answer's generic "price"/"total" branches
                # always answer with the pinned product and this lookup never runs.
                if not _aq_answer and client:
                    try:
                        _aq_all_prods = await catalogue_service.list_products(db, client.id)
                        _aq_scored = catalogue_service.search_products_with_scores(
                            _aq_all_prods, user_text, top_k=3
                        )
                        _aq_pinned_sku = getattr(conv, "pending_product_sku", None)
                        for _aq_sc, _aq_cp in _aq_scored:
                            if _aq_sc >= 3 and getattr(_aq_cp, "sku", None) != _aq_pinned_sku:
                                _aq_answer = (
                                    f"{_aq_cp.name} — ₹{int(getattr(_aq_cp, 'price', 0) or 0):,}."
                                )
                                break
                    except Exception as _aqe:
                        logger.warning("FIX4 cross-product lookup failed: %s", _aqe)

                # Issue A: KB takes priority over the deterministic fact table —
                # a proven past answer is more specific than a generic template.
                if not _aq_answer and client:
                    try:
                        from app.services import knowledge_service as _aq_kb
                        _aq_kb_entries = await _aq_kb.search_knowledge(
                            client_id=client.id, query=user_text, db=db,
                        )
                        if _aq_kb_entries:
                            _aq_answer = _aq_kb_entries[0].answer
                            try:
                                for _e in _aq_kb_entries:
                                    _e.usage_count += 1
                                await db.commit()
                            except Exception:
                                pass
                    except Exception as _aq_kb_exc:
                        logger.warning("FIX2 KB search failed: %s", _aq_kb_exc)

                if not _aq_answer:
                    _aq_answer = _build_order_aside_answer(user_text, conv, client, pinned_product, _aq_dt)

                if _aq_answer:
                    _aq_slot_q = _build_slot_question(
                        _next_slot_pre, conv, variant_info, _aq_lang,
                        customer_profile=customer_profile,
                        accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                        available_stock=available_stock,
                        product_name=_aq_prod_name,
                        declined_saved_address=False,  # question, not a "change" negation
                        attempt_count=_current_slot_attempts,
                    )
                    _aq_order_ctx = ""
                    if getattr(conv, "pending_order_quantity", None):
                        _aq_order_ctx = f"Your current order: {_aq_prod_name} x{conv.pending_order_quantity}. "
                    _aq_reply = f"{_aq_answer}\n\n{_aq_order_ctx}{_aq_slot_q}" if _aq_slot_q else _aq_answer
                    logger.info(
                        "Question at order stage → answered from KB/LLM, re-prompting conv=%s",
                        conv.id,
                    )
                    logger.info(
                        "FIX2 aside-question: conv=%s slot=%r — answered + re-asked, no state change",
                        conv.id, _next_slot_pre,
                    )
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _aq_reply)
                    except Exception:
                        pass
                    try:
                        await whatsapp_service.send_text_message(sender_phone, _aq_reply)
                    except Exception:
                        pass
                    try:
                        await _record_usage(db, client)
                    except Exception:
                        pass
                    return {"status": "ok"}
                # No deterministic answer — fall through to normal intent classification.

            # ── Intent classification ──────────────────────────────────────────
            _product_name = getattr(pinned_product, "name", None) or getattr(conv, "pending_product_sku", "current product") or "current product"

            # P0-2: deterministic fast-path — a trivial slot answer (color/size/
            # qty/yes-no/free-text address) never needs an LLM classify call.
            # Skip straight to ANSWER + extraction when the message:
            #   - contains no price-objection / cancel keywords
            #   - is not a bare SKU token belonging to a DIFFERENT product
            #   - cleanly resolves via the deterministic extractor for the
            #     current slot
            _det_skip_llm = False
            if (
                message.type == "text"
                and not conversation_flow._is_price_objection(user_text)
                and not any(
                    kw in user_text.lower()
                    for kw in ("cancel", "ruk jao", "band karo", "rok do", "રદ કરો", "रद्द", "cancel karo", "nahi chahiye", "nahi karna")
                )
            ):
                _det_probe = conversation_flow.extract_order_field(
                    conv, user_text,
                    variant_info=variant_info,
                    conversation_history=history_dicts,
                    available_stock=available_stock,
                    saved_address=getattr(customer_profile, "address", None) if customer_profile else None,
                )
                if _det_probe is not None:
                    _det_skip_llm = True

            if _det_skip_llm:
                _intent = "ANSWER"
                logger.info(
                    "conv=%s deterministic slot answer %r → ANSWER, next_slot=%s — no LLM classify call",
                    conv.id, user_text[:40], _next_slot_pre,
                )
                _intent_struct = {"intent": "ANSWER", "entities": {}}
            else:
                try:
                    _intent_struct = await conversation_flow.classify_user_intent(
                        user_text, _next_slot_pre, _product_name, conversation_id=conv.id
                    )
                    _intent = _intent_struct["intent"]
                    _llm_called_this_turn = True
                except Exception as exc:
                    logger.warning("Intent classification error (defaulting ANSWER): %s", exc)
                    _intent = "ANSWER"

            _classified_intent = _intent
            logger.info("conv=%s intent=%s next_slot=%s", conv.id, _intent, _next_slot_pre)

            if _intent == "NEW_PRODUCT":
                # Auto-switch silently: pin new product, keep name+address, reset variant/qty
                from app.services.catalogue_service import extract_skus_from_text as _extract_skus
                _mentioned_skus = _extract_skus(user_text)
                _new_sku_candidate = _mentioned_skus[0] if _mentioned_skus else None
                if _new_sku_candidate and client:
                    _switch_product = await catalogue_service.find_product_by_sku(db, client.id, _new_sku_candidate)
                    if _switch_product and await _is_product_out_of_stock(db, _switch_product):
                        # OOS gate: don't switch, just report sold out
                        _oos_sw_name = getattr(_switch_product, "name", _new_sku_candidate)
                        _classified_intent = "NEW_PRODUCT_OOS"
                        _intent_override = f"OOS:{_oos_sw_name}"
                        logger.info("conv=%s NEW_PRODUCT switch blocked — OOS: %s", conv.id, _new_sku_candidate)
                    else:
                        # Track old SKU as browsed before switching
                        _old_pinned = getattr(conv, "pending_product_sku", None)
                        if _old_pinned and _old_pinned != _new_sku_candidate:
                            import json as _json
                            _browsed_raw = getattr(conv, "browsed_skus", None) or "[]"
                            try:
                                _browsed = _json.loads(_browsed_raw)
                            except Exception:
                                _browsed = []
                            if _old_pinned not in _browsed:
                                _browsed.append(_old_pinned)
                            try:
                                _nb = _json.dumps(_browsed)
                                await conversation_service.update_order_field(db, conv.id, "browsed_skus", _nb)
                                conv.browsed_skus = _nb
                            except Exception as exc:
                                logger.error("browsed_skus update (auto-switch): %s", exc)
                        # Reset variant/qty/summary; keep customer_name + delivery_address
                        _sw_reset_fields = [
                            ("pending_order_quantity", None), ("selected_color", None),
                            ("selected_size", None), ("selected_material", None),
                            ("summary_shown", False),
                        ]
                        for _rf, _rv in _sw_reset_fields:
                            try:
                                await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                                setattr(conv, _rf, _rv)
                            except Exception as exc:
                                logger.error("auto-switch slot reset (%s): %s", _rf, exc)
                        try:
                            await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _new_sku_candidate)
                            conv.pending_product_sku = _new_sku_candidate
                            await conversation_service.update_stage(db, conv.id, "order_collection")
                            conv.current_stage = "order_collection"
                            stage = "order_collection"
                        except Exception as exc:
                            logger.error("auto-switch pin error: %s", exc)
                        if _switch_product:
                            pinned_product = _switch_product
                        if pinned_product:
                            try:
                                variant_info = await catalogue_service.get_product_variant_info(db, pinned_product)
                            except Exception as exc:
                                logger.error("auto-switch variant_info re-fetch: %s", exc)
                        _classified_intent = "AUTO_SWITCH_DONE"
                        _switch_resolved = True
                        logger.info("conv=%s NEW_PRODUCT auto-switched to %s (was %s)", conv.id, _new_sku_candidate, _old_pinned)
                else:
                    # No SKU found in message — treat as OTHER (re-ask slot)
                    _intent_override = "OTHER"

            elif _intent == "CANCEL":
                # Cancel any pending_payment order first (BUG 1 FIX)
                from app.models.order import Order as _CancelOrderModel2
                try:
                    _co2_result = await db.execute(
                        select(_CancelOrderModel2)
                        .where(
                            _CancelOrderModel2.conversation_id == conv.id,
                            _CancelOrderModel2.status == "pending_payment",
                        )
                        .order_by(_CancelOrderModel2.created_at.desc())
                        .limit(1)
                    )
                    _order_to_cancel2 = _co2_result.scalar_one_or_none()
                    if _order_to_cancel2:
                        _order_to_cancel2.status = "cancelled"
                        await db.commit()
                        logger.info(
                            "Order %s → cancelled (conv=%s) [CANCEL intent]",
                            _order_to_cancel2.order_number, conv.id,
                        )
                except Exception as exc:
                    logger.error("CANCEL intent: order cancellation failed for conv=%s: %s", conv.id, exc)

                # Reset all order slots and go back to greeting
                _cancel_fields = [
                    ("pending_order_quantity", None), ("selected_color", None),
                    ("selected_size", None), ("selected_material", None),
                    ("customer_name", None), ("delivery_address", None),
                    ("payment_method", None), ("summary_shown", False),
                    ("pending_product_sku", None), ("interrupted_sku", None),
                    ("last_shown_sku", None), ("pending_choice_skus", None),
                ]
                for _rf, _rv in _cancel_fields:
                    try:
                        await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                        setattr(conv, _rf, _rv)
                    except Exception as exc:
                        logger.error("Cancel slot reset (%s): %s", _rf, exc)
                try:
                    await conversation_service.update_stage(db, conv.id, "greeting")
                    stage = "greeting"
                    conv.current_stage = "greeting"
                except Exception as exc:
                    logger.error("Cancel stage reset error: %s", exc)
                logger.info("conv=%s CANCEL — all slots reset, stage=greeting", conv.id)

            elif _intent == "DISCOUNT_QUERY":
                # Slot state is NOT touched — we resume exactly where we left off.
                logger.info("conv=%s DISCOUNT_QUERY — will show fixed-price reply + re-ask slot=%s", conv.id, _next_slot_pre)

            elif _intent == "OFF_TOPIC":
                # Improvement 2: track off-topic abuse
                _midorder_ot_count = (conv.off_topic_count or 0) + 1
                conv.off_topic_count = _midorder_ot_count
                try:
                    await conversation_service.update_order_field(db, conv.id, "off_topic_count", _midorder_ot_count)
                except Exception:
                    pass
                _ot_threshold_mo = int(getattr(client, "off_topic_threshold", None) or _DEFAULT_OFF_TOPIC_THRESHOLD) if client else _DEFAULT_OFF_TOPIC_THRESHOLD
                _shop_name_mo = (getattr(client, "business_name", None) or "our shop") if client else "our shop"
                if _midorder_ot_count >= _ot_threshold_mo:
                    _ot_boundary_mo = f"I can only help with orders from {_shop_name_mo}. Tap a product or type 'cancel' to start over."
                    logger.warning("OFF_TOPIC threshold (mid-order): conv=%s count=%d — boundary reply.", conv.id, _midorder_ot_count)
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _ot_boundary_mo)
                    except Exception:
                        pass
                    try:
                        await whatsapp_service.send_text_message(sender_phone, _ot_boundary_mo)
                    except Exception:
                        pass
                    try:
                        await _record_usage(db, client)
                    except Exception:
                        pass
                    return {"status": "ok"}
                # Mid-order off-topic: deflect + re-ask current slot without touching slot state.
                _ot_lang = getattr(conv, "last_customer_language", None) or language or "english"
                from app.services.language_templates import get_template as _get_ot_tpl
                _ot_prod_name_ot = getattr(pinned_product, "name", "the product") or "the product"
                _ot_slot_q = _build_slot_question(
                    _next_slot_pre, conv, variant_info, _ot_lang,
                    customer_profile=customer_profile,
                    accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                    available_stock=available_stock,
                    product_name=_ot_prod_name_ot,
                )
                # FIX 5: simple acks ("got it", "achha got it") at a slot should get
                # a short forward-moving prompt, not the full off-topic deflect template.
                if _is_simple_ack(user_text):
                    _ot_reply = f"No problem! {_ot_slot_q}" if _ot_slot_q else "No problem! Let me know when you're ready."
                    logger.info("FIX5 ack at slot: conv=%s slot=%r — forward-moving prompt", conv.id, _next_slot_pre)
                else:
                    _ot_reply = _get_ot_tpl(
                        _ot_lang, "off_topic_midorder",
                        slot_question=_ot_slot_q,
                    )
                logger.info("conv=%s OFF_TOPIC (mid-order) — deflect + re-ask slot=%s", conv.id, _next_slot_pre)
                try:
                    await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                    await conversation_service.save_message(db, conv.id, "assistant", _ot_reply)
                except Exception as exc:
                    logger.error("OFF_TOPIC mid-order save error: %s", exc)
                try:
                    await whatsapp_service.send_text_message(sender_phone, _ot_reply)
                except Exception as exc:
                    logger.error("OFF_TOPIC mid-order send error: %s", exc)
                try:
                    await _record_usage(db, client)
                except Exception:
                    pass
                return {"status": "ok"}

            elif _intent == "OTHER":
                # Skip extraction — AI answers the question, then slot re-ask is appended.
                _intent_override = "OTHER"  # sentinel for the reply-building section
                logger.info("conv=%s OTHER — skipping extraction, will re-ask slot", conv.id)

            else:  # ANSWER — normal slot extraction
                extracted = conversation_flow.extract_order_field(
                    conv,
                    user_text,
                    variant_info=variant_info,
                    conversation_history=history_dicts,
                    available_stock=available_stock,
                    saved_address=getattr(customer_profile, "address", None) if customer_profile else None,
                )
                if extracted:
                    field, value = extracted
                    if field == "quantity_invalid":
                        _quantity_invalid = True
                        logger.info(
                            "Quantity %s exceeds available stock %s for conv %s — re-prompting.",
                            getattr(conv, "pending_order_quantity", "?"),
                            value,
                            conv.id,
                        )
                    else:
                        # ── Phase 3: validate every write against fresh DB facts ─
                        # extract_order_field already validates variant values against
                        # the in-memory variant_info (which is fetched fresh from DB
                        # each turn). The guard below makes validation explicit and
                        # logs rejections so they are visible in monitoring.
                        _write_rejected = False
                        if field == "selected_color":
                            _valid_colors = [c.lower() for c in variant_info.get("available_colors", [])]
                            if _valid_colors and str(value).lower() not in _valid_colors:
                                logger.warning(
                                    "WRITE REJECTED conv=%s field=%r value=%r "
                                    "not in variant DB set %s — re-asking slot.",
                                    conv.id, field, value, _valid_colors,
                                )
                                _write_rejected = True
                        elif field == "selected_size":
                            _valid_sizes = [s.lower() for s in variant_info.get("available_sizes", [])]
                            if _valid_sizes and str(value).lower() not in _valid_sizes:
                                logger.warning(
                                    "WRITE REJECTED conv=%s field=%r value=%r "
                                    "not in variant DB set %s — re-asking slot.",
                                    conv.id, field, value, _valid_sizes,
                                )
                                _write_rejected = True
                        elif field == "selected_material":
                            _valid_mats = [m.lower() for m in variant_info.get("available_materials", [])]
                            if _valid_mats and str(value).lower() not in _valid_mats:
                                logger.warning(
                                    "WRITE REJECTED conv=%s field=%r value=%r "
                                    "not in variant DB set %s — re-asking slot.",
                                    conv.id, field, value, _valid_mats,
                                )
                                _write_rejected = True
                        elif field == "pending_order_quantity":
                            if not isinstance(value, int) or value < 1:
                                logger.warning(
                                    "WRITE REJECTED conv=%s field=%r value=%r "
                                    "must be int ≥ 1 — re-asking slot.",
                                    conv.id, field, value,
                                )
                                _write_rejected = True
                        if _write_rejected:
                            _new_wr = (conv.slot_attempt_count or 0) + 1
                            conv.slot_attempt_count = _new_wr
                            try:
                                await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", _new_wr)
                            except Exception as _wri:
                                logger.error("slot_attempt write-rejected increment: %s", _wri)
                            logger.info("Slot attempt (write-rejected): conv=%s slot=%r attempt=%d value=%r", conv.id, _next_slot_pre, _new_wr, value)
                        if not _write_rejected:
                            try:
                                await conversation_service.update_order_field(db, conv.id, field, value)
                                setattr(conv, field, value)
                                logger.info("Slot filled: conv=%s %s=%r", conv.id, field, value)
                                # Reset slot attempt counter and off_topic_count on successful slot fill
                                try:
                                    await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", 0)
                                    await conversation_service.update_order_field(db, conv.id, "slot_attempt_slot", None)
                                    await conversation_service.update_order_field(db, conv.id, "off_topic_count", 0)
                                    conv.slot_attempt_count = 0
                                    conv.slot_attempt_slot = None
                                    conv.off_topic_count = 0
                                except Exception as _slr:
                                    logger.error("Slot-fill counter reset error: %s", _slr)
                            except Exception as exc:
                                logger.error("Order field update error: %s", exc)
                                _write_rejected = True

                            # Fix A: when color is written (including Fix B color-switch),
                            # immediately re-scope available_sizes to the new color so the
                            # size question on THIS turn uses the correct filtered list.
                            if not _write_rejected and field == "selected_color":
                                if pinned_product and getattr(pinned_product, "has_variants", False):
                                    try:
                                        _rescope = await catalogue_service.get_in_stock_options(
                                            db, pinned_product.id, color=value
                                        )
                                        if _rescope.get("sizes"):
                                            variant_info = {**variant_info, "available_sizes": _rescope["sizes"]}
                                    except Exception as _rse:
                                        logger.warning("Fix A re-scope after color write: %s", _rse)

                        # FIX 1 — post-extraction combo OOS gate.
                        # After writing a variant attribute, check whether the
                        # NOW-complete combo (color+size+material) is actually in
                        # stock.  This catches cases like Pink+XXL=0 that only
                        # become detectable after the LAST attr is filled in the
                        # same turn (the per-turn OOS gate at the top of the
                        # request ran BEFORE extraction, so it could not see this).
                        if (
                            not _write_rejected
                            and field in ("selected_color", "selected_size", "selected_material")
                            and pinned_product is not None
                            and getattr(pinned_product, "has_variants", False)
                        ):
                            _post_color    = getattr(conv, "selected_color",    None)
                            _post_size     = getattr(conv, "selected_size",     None)
                            _post_material = getattr(conv, "selected_material", None)
                            _post_all_filled = (
                                (not variant_info.get("needs_color")    or _post_color)
                                and (not variant_info.get("needs_size")     or _post_size)
                                and (not variant_info.get("needs_material") or _post_material)
                            )
                            if _post_all_filled:
                                try:
                                    _combo_ok, _combo_stock = await catalogue_service.variant_available(
                                        db, pinned_product.id,
                                        color=_post_color, size=_post_size, material=_post_material,
                                    )
                                except Exception as _cvae:
                                    logger.error("Post-extraction combo OOS check failed: %s", _cvae)
                                    _combo_ok, _combo_stock = True, 1  # fail-open
                                if not _combo_ok:
                                    # Roll back the just-written field so the slot
                                    # is asked again with an informative reply.
                                    try:
                                        await conversation_service.update_order_field(db, conv.id, field, None)
                                        setattr(conv, field, None)
                                    except Exception as _cre:
                                        logger.error("Combo OOS rollback error: %s", _cre)
                                    _combo_oos = True
                                    available_stock = None
                                    conv.__dict__["_combo_oos_cleared_field"] = field  # Fix C
                                    # Fetch which options ARE in-stock so the
                                    # reply can list them (e.g. "Pink isn't in
                                    # XXL — available sizes in Pink: S, M, L").
                                    _fixed_color    = _post_color    if field != "selected_color"    else None
                                    _fixed_size     = _post_size     if field != "selected_size"     else None
                                    _fixed_material = _post_material if field != "selected_material" else None
                                    try:
                                        _avail_opts = await catalogue_service.get_in_stock_options(
                                            db, pinned_product.id,
                                            color=_fixed_color, size=_fixed_size, material=_fixed_material,
                                        )
                                    except Exception:
                                        _avail_opts = {}
                                    # Stash so _build_slot_question / language_templates can use it.
                                    conv.__dict__["_combo_oos_avail_opts"] = _avail_opts
                                    logger.info(
                                        "Post-extraction combo OOS: conv=%s combo=%r/%r/%r "
                                        "stock=%d — cleared %s, avail=%s",
                                        conv.id, _post_color, _post_size, _post_material,
                                        _combo_stock, field, _avail_opts,
                                    )
                if not extracted and _next_slot_pre is not None:
                    # Could not extract a value — count as failed attempt
                    _new_noext = (conv.slot_attempt_count or 0) + 1
                    conv.slot_attempt_count = _new_noext
                    try:
                        await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", _new_noext)
                    except Exception as _nei:
                        logger.error("slot_attempt no-extract increment: %s", _nei)
                    logger.info("Slot attempt (no-extract): conv=%s slot=%r attempt=%d", conv.id, _next_slot_pre, _new_noext)
                    # FIX 1: for address slot, send the specific rejection message
                    # immediately (before the normal template render at the bottom)
                    # so the customer knows WHY the address was rejected.
                    if _next_slot_pre == "delivery_address" and message.type == "text":
                        _addr_rej_lang = getattr(conv, "last_customer_language", None) or language or "english"
                        # Diagnose the SPECIFIC thing missing — "too_short" (no real
                        # address text), "no_structure" (no digits/comma at all), or
                        # "bad_pincode" (a digit run that isn't exactly 6 digits) —
                        # so the customer knows exactly what to fix (P0-1.3).
                        _addr_reason = conversation_flow.classify_address_rejection(user_text)
                        _ADDR_REJ_MSGS = {
                            "english": {
                                "bad_pincode": "That pincode looks short — I need a 6-digit pincode. What's the full address with pincode?",
                                "no_structure": "I need your full delivery address — house/area, city and a 6-digit pincode.",
                                "too_short": "That doesn't look like a complete address. Please send house/area, city and a 6-digit pincode.",
                            },
                            "hindi": {
                                "bad_pincode": "Yeh pincode chhota lag raha hai — mujhe 6-digit pincode chahiye. Pura address pincode ke saath bhejein.",
                                "no_structure": "Mujhe pura delivery address chahiye — ghar/area, shahar aur 6-digit pincode.",
                                "too_short": "Yeh address incomplete lagta hai. Kripaya ghar/area, shahar aur 6-digit pincode ke saath poora address bhejein.",
                            },
                            "gujarati": {
                                "bad_pincode": "Aa pincode tunko lagi rahyo che — mane 6-digit pincode joiye. Pooro address pincode saathe moklo.",
                                "no_structure": "Mane tamaru pooru delivery address joiye — ghar/area, shaher ane 6-digit pincode.",
                                "too_short": "Aa address adhuro lagche. Meherbani kari ghar/area, shaher ane 6-digit pincode saathe pooro address moklo.",
                            },
                        }
                        if _addr_rej_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                            _addr_rej = _ADDR_REJ_MSGS["hindi"][_addr_reason]
                        elif _addr_rej_lang in ("gujarati_roman", "gujarati_script"):
                            _addr_rej = _ADDR_REJ_MSGS["gujarati"][_addr_reason]
                        else:
                            _addr_rej = _ADDR_REJ_MSGS["english"][_addr_reason]
                        logger.info(
                            "FIX1 address rejection: conv=%s attempt=%d text=%r",
                            conv.id, _new_noext, user_text[:60],
                        )
                        try:
                            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                            await conversation_service.save_message(db, conv.id, "assistant", _addr_rej)
                        except Exception:
                            pass
                        try:
                            await whatsapp_service.send_text_message(sender_phone, _addr_rej)
                        except Exception:
                            pass
                        try:
                            await _record_usage(db, client)
                        except Exception:
                            pass
                        return {"status": "ok"}
        else:
            # next_slot is None (all slots filled) — no extraction needed
            pass

    # ── Determine next_slot AFTER extraction (slot may have advanced) ──────────
    # Assertion: pinned_product.sku must always match conv.pending_product_sku.
    # A mismatch means variant_info is stale and slot decisions will be wrong.
    if pinned_product and getattr(conv, "pending_product_sku", None):
        if getattr(pinned_product, "sku", None) != conv.pending_product_sku:
            logger.error(
                "variant_info/product MISMATCH — pinned_product.sku=%r != "
                "conv.pending_product_sku=%r — variant_info is stale. "
                "This should never happen after the Fix 1 restructure.",
                getattr(pinned_product, "sku", None),
                conv.pending_product_sku,
            )
    logger.info(
        "SLOT-DEBUG conv=%s stage=%s variant_info=%s | qty=%r color=%r size=%r material=%r name=%r address=%r payment=%r",
        conv.id, stage, variant_info,
        getattr(conv, "pending_order_quantity", None),
        getattr(conv, "selected_color", None),
        getattr(conv, "selected_size", None),
        getattr(conv, "selected_material", None),
        getattr(conv, "customer_name", None),
        getattr(conv, "delivery_address", None),
        getattr(conv, "payment_method", None),
    )
    _next_slot: str | None = conversation_flow.get_next_required_slot(conv, variant_info)
    logger.info("SLOT-DEBUG conv=%s next_slot=%r", conv.id, _next_slot)

    # ── Auto-fill payment_method=UPI for UPI-only clients ─────────────────────
    # When the next slot is payment_method and the client doesn't accept COD,
    # fill it automatically — no need to ask the customer.
    if (
        _next_slot == "payment_method"
        and client
        and not getattr(client, "accepts_cod", False)
    ):
        try:
            await conversation_service.update_order_field(db, conv.id, "payment_method", "UPI")
            conv.payment_method = "UPI"
            _next_slot = conversation_flow.get_next_required_slot(conv, variant_info)
            logger.info("Auto-filled payment_method=UPI for conv=%s (UPI-only client)", conv.id)
        except Exception as exc:
            logger.error("Auto-fill UPI payment_method error: %s", exc)

    # When quantity was invalid we stay in order_collection but tell the AI
    # to re-ask with the stock limit rather than asking the next slot.
    if _quantity_invalid:
        _next_slot = "quantity_invalid"

    # When a variant combo is OOS, override next_slot to re-ask the cleared attr.
    if _combo_oos:
        _next_slot = "combo_oos"

    # ── Declined saved-address flag ───────────────────────────────────────────
    # True when: the agent just offered the saved address AND the customer
    # replied with a negation ("change", "no", "nahi" …). This ensures the
    # current reply asks for a fresh address instead of looping on the confirm.
    _declined_saved_address = (
        _next_slot == "delivery_address"
        and customer_profile is not None
        and bool(getattr(customer_profile, "address", None))
        and conversation_flow._last_agent_offered_saved_address(history_dicts)
        and user_text.lower().strip() in conversation_flow._SAVED_ADDRESS_NEGATIONS
    )

    # ── First-slot flag (Issue 3: pin confirmation prefix) ────────────────────
    # True when we are about to ask the VERY FIRST slot question for the pinned
    # product — all order slots are empty, meaning nothing has been collected yet.
    _is_first_slot = (
        stage == "order_collection"
        and pinned_product is not None
        and not getattr(conv, "selected_color", None)
        and not getattr(conv, "selected_size", None)
        and not getattr(conv, "selected_material", None)
        and not (getattr(conv, "pending_order_quantity", None) or 0)
        and not getattr(conv, "customer_name", None)
        and not getattr(conv, "delivery_address", None)
        and not getattr(conv, "payment_method", None)
    )

    # ── Confirmation guard: slots must be 100% complete before advancing ─────
    # detect_stage() or a button tap may have set stage="awaiting_final_confirmation"
    # or stage="completed" from a "yes"/"Confirm Order" signal, but if next_slot
    # is not None there are still required fields missing.  Block the advance and
    # force order_collection so the AI re-asks the missing slot instead.
    _confirmation_gate_fired = False
    if stage in ("awaiting_final_confirmation", "completed") and _next_slot is not None:
        logger.warning(
            "Confirmation blocked — slots incomplete (next_slot=%r) for conv=%s "
            "(stage was %r, reverting to order_collection). "
            "Slots: color=%r size=%r material=%r qty=%r name=%r address=%r payment=%r",
            _next_slot, conv.id, stage,
            getattr(conv, "selected_color", None),
            getattr(conv, "selected_size", None),
            getattr(conv, "selected_material", None),
            getattr(conv, "pending_order_quantity", None),
            getattr(conv, "customer_name", None),
            getattr(conv, "delivery_address", None),
            getattr(conv, "payment_method", None),
        )
        stage = "order_collection"
        _confirmation_gate_fired = True
        # _intent_override will be set from get_next_slot_prompt_instruction below

    # If all slots are filled and we're still in order_collection, advance
    # stage to awaiting_final_confirmation so detect_stage locks correctly.
    if stage == "order_collection" and _next_slot is None:
        stage = "awaiting_final_confirmation"

    # ── Build slot-machine instruction for the system prompt ───────────────────
    # _intent_override takes priority: it's set when the classifier detected
    # NEW_PRODUCT / CANCEL / OTHER so the AI knows what to do this turn.
    _current_instruction = ""
    if _intent_override:
        _current_instruction = _intent_override
    elif stage in ("order_collection", "awaiting_final_confirmation"):
        _current_instruction = conversation_flow.get_next_slot_prompt_instruction(
            next_slot=_next_slot,
            variant_info=variant_info,
            product=pinned_product,
            available_stock=available_stock,
            language=language,
        )

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

    # Section 4: learning_service "similar past conversations" are deliberately
    # NOT injected into the live prompt — they add tokens and can pull a wrong
    # product/price from an old chat (the same failure class as the captured
    # wrong-product bug). learning_service remains available for offline
    # analytics only; it must never be imported on this live request path.

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
    _delivery_time = get_delivery_time_str(pinned_product, client)
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
            current_instruction=_current_instruction,
            delivery_time=_delivery_time,
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

    # ── Catalogue link request ────────────────────────────────────────────────
    # Detected keywords like "catalogue", "full list", "shop link", etc.
    # Short-circuit the AI call and send the shop URL directly.
    if (
        message.type in ("text", "interactive")
        and conversation_flow.is_catalogue_request(user_text)
        and client
    ):
        _cat_slug = getattr(client, "catalogue_slug", None)
        _cat_url = (
            f"{settings.catalogue_base_url}/{_cat_slug}"
            if _cat_slug
            else settings.catalogue_base_url
        )
        _business = getattr(client, "business_name", "our store") or "our store"
        _lang_cat = getattr(conv, "last_customer_language", "english") or "english"
        if _lang_cat in ("hindi_roman", "hindi_devanagari", "hinglish"):
            _cat_reply = (
                f"Zaroor! Hamara poora collection yahan dekh sakte hain 🛍️\n"
                f"{_cat_url}\n\nKoi bhi product pasand aaye toh SKU ya naam bhejein, "
                f"main details share karta hoon! 😊"
            )
        elif _lang_cat in ("gujarati_roman", "gujarati_script"):
            _cat_reply = (
                f"Jarur! Amaru pooru collection aaiya joi shakay chho 🛍️\n"
                f"{_cat_url}\n\nKoi product game to SKU ya naam moklo, "
                f"hu details share karish! 😊"
            )
        else:
            _cat_reply = (
                f"Here's our complete collection 🛍️\n"
                f"{_cat_url}\n\nIf any product catches your eye, just send the SKU or name "
                f"and I'll share full details! 😊"
            )
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _cat_reply)
        except Exception as exc:
            logger.error("Catalogue reply save error: %s", exc)
        try:
            await conversation_service.update_stage(db, conv.id, stage)
        except Exception as exc:
            logger.error("Stage update error (catalogue): %s", exc)
        try:
            await whatsapp_service.send_text_message(sender_phone, _cat_reply)
        except Exception as exc:
            logger.error("Catalogue link send error: %s", exc)
        try:
            await _record_usage(db, client)
        except Exception as exc:
            logger.error("Usage tracking error (catalogue): %s", exc)
        return {"status": "ok"}

    # ── Greeting short-circuit (proactive catalogue link) ─────────────────────
    # Pure greeting (Hi/Hello/Namaste/…) with no active product → skip AI,
    # send a deterministic welcome + catalogue link immediately.
    # Mid-order greetings (pending_product_sku set) fall through to normal flow.
    _GREETING_WORDS = frozenset({
        "hi", "hello", "hey", "namaste", "hii", "helo", "hiii",
        "namaskar", "namaskte", "kem cho", "salam", "assalam",
    })
    _is_pure_greeting = (
        message.type == "text"
        and bool(user_text)
        and set(user_text.lower().strip().split()) & _GREETING_WORDS
        and not getattr(conv, "pending_product_sku", None)
        and client
    )
    if _is_pure_greeting:
        _g_slug = getattr(client, "catalogue_slug", None)
        _g_cat_url = (
            f"{settings.catalogue_base_url}/{_g_slug}"
            if _g_slug
            else settings.catalogue_base_url
        )
        _g_business = getattr(client, "business_name", "our store") or "our store"
        _g_lang = (getattr(conv, "last_customer_language", None) or language or "english").lower()
        from app.services.language_templates import get_template as _get_gtpl
        _cp_name = (
            getattr(customer_profile, "name", None)
            if customer_profile and (customer_profile.total_orders or 0) > 0
            else None
        )
        if _cp_name:
            _g_reply = _get_gtpl(
                _g_lang, "greeting_returning",
                name=_cp_name, catalogue_url=_g_cat_url,
            )
        else:
            _g_reply = _get_gtpl(
                _g_lang, "greeting_new",
                business=_g_business, catalogue_url=_g_cat_url,
            )
        _log_route(conv.id, "TEMPLATE", "greeting_short_circuit", extra=f"lang={_g_lang}")
        logger.info(
            "Greeting short-circuit: conv=%s returning=%s lang=%s — no AI call.",
            conv.id, bool(_cp_name), _g_lang,
        )
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _g_reply)
        except Exception as exc:
            logger.error("Greeting save error: %s", exc)
        try:
            await conversation_service.update_stage(db, conv.id, "greeting")
        except Exception as exc:
            logger.error("Greeting stage update error: %s", exc)
        try:
            await whatsapp_service.send_text_message(sender_phone, _g_reply)
        except Exception as exc:
            logger.error("Greeting send error: %s", exc)
        try:
            await _record_usage(db, client)
        except Exception as exc:
            logger.error("Usage tracking error (greeting): %s", exc)
        return {"status": "ok"}

    # ── OFF_TOPIC short-circuit (idle + completed stages) ────────────────────
    # Mid-order OFF_TOPIC is handled earlier in the intent-classification block.
    # Here we catch off-topic messages when there is NO active order in progress
    # (idle stages: greeting, product_inquiry, qualification, objection_handling,
    # offer_making) and when the order is completed (post-completion safety).
    #
    # POST-COMPLETION SAFETY: completed + non-buy message → idle deflect, never
    # starts a new order. Only a real product/SKU match or explicit buy intent
    # (handled earlier in the SKU-match and name-match blocks) starts a new order.
    _IDLE_STAGES_FOR_OT = frozenset({
        "greeting", "product_inquiry", "qualification",
        "objection_handling", "offer_making",
    })
    _is_idle_or_completed = stage in _IDLE_STAGES_FOR_OT or stage == "completed"
    if (
        message.type == "text"
        and user_text
        and _is_idle_or_completed
        and client
    ):
        _ot_product_ctx = getattr(pinned_product, "name", None) if pinned_product else None
        try:
            _is_ot = await conversation_flow.is_off_topic_message(user_text, stage, _ot_product_ctx, conversation_id=conv.id)
            _llm_called_this_turn = True
        except Exception as _ot_exc:
            logger.warning("is_off_topic_message error (skipping): %s", _ot_exc)
            _is_ot = False

        if _is_ot:
            # Improvement 2: track off-topic abuse (idle)
            _idle_ot_count = (conv.off_topic_count or 0) + 1
            conv.off_topic_count = _idle_ot_count
            try:
                await conversation_service.update_order_field(db, conv.id, "off_topic_count", _idle_ot_count)
            except Exception:
                pass
            _ot_threshold_idle = int(getattr(client, "off_topic_threshold", None) or _DEFAULT_OFF_TOPIC_THRESHOLD) if client else _DEFAULT_OFF_TOPIC_THRESHOLD
            _shop_name_idle = (getattr(client, "business_name", None) or "our shop") if client else "our shop"
            if _idle_ot_count >= _ot_threshold_idle:
                _boundary_idle = f"I can only help with orders from {_shop_name_idle}. Tap a product or type 'cancel' to start over."
                logger.warning("OFF_TOPIC threshold (idle): conv=%s count=%d — boundary reply.", conv.id, _idle_ot_count)
                try:
                    await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                    await conversation_service.save_message(db, conv.id, "assistant", _boundary_idle)
                except Exception:
                    pass
                try:
                    await whatsapp_service.send_text_message(sender_phone, _boundary_idle)
                except Exception:
                    pass
                try:
                    await _record_usage(db, client)
                except Exception:
                    pass
                return {"status": "ok"}
            _ot_idle_lang = getattr(conv, "last_customer_language", None) or language or "english"
            from app.services.language_templates import get_template as _get_ot_idle_tpl
            # Build contact line: website (catalogue URL) + phone, omitting blank lines
            _ot_cat_slug = getattr(client, "catalogue_slug", None)
            _ot_cat_url = (
                f"{settings.catalogue_base_url}/{_ot_cat_slug}"
                if _ot_cat_slug else settings.catalogue_base_url
            ) if _ot_cat_slug else None
            _ot_phone = getattr(client, "phone", None)
            _contact_parts = []
            if _ot_cat_url:
                if _ot_idle_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                    _contact_parts.append(f"Humari products ke baare mein jaankari ke liye hamara catalogue dekhein: {_ot_cat_url}")
                elif _ot_idle_lang in ("gujarati_roman", "gujarati_script"):
                    _contact_parts.append(f"Amara products vishe mahiti mate amaru catalogue joi lo: {_ot_cat_url}")
                else:
                    _contact_parts.append(f"For info about our products, visit our catalogue: {_ot_cat_url}")
            if _ot_phone:
                if _ot_idle_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                    _contact_parts.append(f"Ya call karein: {_ot_phone}")
                elif _ot_idle_lang in ("gujarati_roman", "gujarati_script"):
                    _contact_parts.append(f"Ya call karo: {_ot_phone}")
                else:
                    _contact_parts.append(f"Or call us: {_ot_phone}")
            _contact_line = "\n".join(_contact_parts) + "\n\n" if _contact_parts else ""
            _ot_idle_reply = _get_ot_idle_tpl(
                _ot_idle_lang, "off_topic_idle",
                contact_line=_contact_line,
            )
            logger.info(
                "OFF_TOPIC idle short-circuit: conv=%s stage=%s — idle deflect, no order started.",
                conv.id, stage,
            )
            try:
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", _ot_idle_reply)
            except Exception as exc:
                logger.error("OFF_TOPIC idle save error: %s", exc)
            try:
                await whatsapp_service.send_text_message(sender_phone, _ot_idle_reply)
            except Exception as exc:
                logger.error("OFF_TOPIC idle send error: %s", exc)
            try:
                await _record_usage(db, client)
            except Exception:
                pass
            return {"status": "ok"}

    # ── Part 4: Order status short-circuit ────────────────────────────────────
    # Keyword-detected "order details / status" queries are answered from DB,
    # NEVER by AI so the model cannot hallucinate order numbers or amounts.
    _ORDER_STATUS_KW = frozenset({
        "order details", "order status", "where is my order", "my order",
        "order kahan hai", "order ka status", "mera order", "order detail",
        "order info", "track order", "order track", "status of my order",
        "order number", "order no",
    })
    _is_order_status_query = (
        message.type in ("text", "interactive")
        and any(kw in user_text.lower() for kw in _ORDER_STATUS_KW)
        and client
    )
    if _is_order_status_query:
        from app.models.order import Order as _OrderStatusModel
        from sqlalchemy import desc as _desc
        _os_result = await db.execute(
            select(_OrderStatusModel)
            .where(_OrderStatusModel.conversation_id == conv.id)
            .order_by(_desc(_OrderStatusModel.created_at))
            .limit(1)
        )
        _os_order = _os_result.scalar_one_or_none()
        _os_lang = getattr(conv, "last_customer_language", None) or language or "english"
        from app.services.language_templates import get_template as _get_ostpl
        from app.services.delivery_service import get_delivery_time_str as _get_dt
        if _os_order:
            _os_product = None
            if getattr(_os_order, "product_id", None):
                _os_product = await catalogue_service.find_product_by_sku(
                    db, client.id, _os_order.product_sku or ""
                ) if _os_order.product_sku else None
            _os_variant_parts = []
            if getattr(_os_order, "variant_color", None):
                _os_variant_parts.append(_os_order.variant_color)
            if getattr(_os_order, "variant_size", None):
                _os_variant_parts.append(_os_order.variant_size)
            if getattr(_os_order, "variant_material", None):
                _os_variant_parts.append(_os_order.variant_material)
            _os_variant_str = (", ".join(_os_variant_parts) + " ") if _os_variant_parts else ""
            _os_dt = _get_dt(_os_product, client)
            _os_reply = _get_ostpl(
                _os_lang, "order_status",
                order_id=_os_order.order_number or str(_os_order.id),
                product=_os_order.product_name or "Product",
                variant_part=_os_variant_str,
                qty=_os_order.quantity or 1,
                total=format_price(_os_order.total_amount or 0),
                status=(_os_order.status or "pending").title(),
                address=_os_order.delivery_address or "—",
                delivery_time=_os_dt,
            )
        else:
            _cat_slug_os = getattr(client, "catalogue_slug", None)
            _cat_url_os = (
                f"{settings.catalogue_base_url}/{_cat_slug_os}"
                if _cat_slug_os else settings.catalogue_base_url
            )
            _os_reply = _get_ostpl(_os_lang, "no_orders", catalogue_url=_cat_url_os)
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _os_reply)
        except Exception as exc:
            logger.error("Order status save error: %s", exc)
        try:
            await whatsapp_service.send_text_message(sender_phone, _os_reply)
        except Exception as exc:
            logger.error("Order status send error: %s", exc)
        try:
            await _record_usage(db, client)
        except Exception as exc:
            logger.error("Usage tracking error (order status): %s", exc)
        _log_route(conv.id, "TEMPLATE", "order_status_short_circuit", extra=f"found={bool(_os_order)}")
        logger.info("Order status short-circuit: conv=%s found=%s", conv.id, bool(_os_order))
        return {"status": "ok"}

    transcribed_text: str | None = None
    original_type: str | None = None

    try:
        if message.type == "image" and message.image is not None:
            logger.info("Image message from %s, media_id=%s", sender_phone, message.image.id)

            # Part 1: image during order_collection — deterministic re-ask, no AI call.
            # Image content never fills an order slot.
            _in_order_flow = (
                stage in ("order_collection", "awaiting_final_confirmation")
                and _next_slot is not None
            )
            if _in_order_flow:
                _img_tpl_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _img_slot_q = _build_slot_question(
                    _next_slot, conv, variant_info, _img_tpl_lang, customer_profile,
                    accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                    available_stock=available_stock,
                    product_name=getattr(pinned_product, "name", "the product") if pinned_product else "the product",
                )
                ai_reply = f"👍\n\n{_img_slot_q}" if _img_slot_q else "👍"
                original_type = "image"
            else:
                image_bytes = await vision_service.download_whatsapp_media(message.image.id)
                # Image messages carry no product code in user_text ("[image]"), so
                # the keyword-search catalogue_context is usually empty. Pass the
                # FULL catalogue instead — a customer asking "is this available?" about
                # a photo should get a matched OR similar product suggestion, never a
                # flat "no product code found" dead end.
                image_catalogue_context = catalogue_context
                if not image_catalogue_context and client:
                    try:
                        all_products = await catalogue_service.list_products(db, client.id)
                        image_catalogue_context = catalogue_service.format_catalogue_context(
                            all_products, for_display=True
                        )
                    except Exception:
                        image_catalogue_context = ""
                ai_reply = await vision_service.analyze_product_image(
                    image_bytes,
                    image_catalogue_context or "",
                )
                original_type = "image"

            # Parse MATCHED_SKU prefix emitted by vision_service prompt.
            # Strip it from the customer-facing reply and use it for SKU pinning.
            import re as _re_img
            _sku_prefix_match = _re_img.match(r"\[MATCHED_SKU:([A-Za-z0-9]+)\]\s*", ai_reply)
            if _sku_prefix_match:
                ai_reply = ai_reply[_sku_prefix_match.end():]
                _vision_sku = _sku_prefix_match.group(1).upper()
                if _vision_sku != "NONE" and client:
                    _vision_browsing = (conv.current_stage or "greeting") not in (
                        "order_collection", "awaiting_final_confirmation", "payment", "completed"
                    )
                    if _vision_browsing:
                        _v_product = await catalogue_service.find_product_by_sku(
                            db, client.id, _vision_sku
                        )
                        if _v_product:
                            _img_reset_fields = [
                                ("pending_order_quantity", None), ("selected_color", None),
                                ("selected_size", None), ("selected_material", None),
                                ("customer_name", None), ("delivery_address", None),
                                ("payment_method", None), ("summary_shown", False),
                            ]
                            for _rf, _rv in _img_reset_fields:
                                try:
                                    await conversation_service.update_order_field(
                                        db, conv.id, _rf, _rv
                                    )
                                    setattr(conv, _rf, _rv)
                                except Exception as _exc:
                                    logger.error("Vision SKU slot reset (%s): %s", _rf, _exc)
                            try:
                                await conversation_service.update_order_field(
                                    db, conv.id, "pending_product_sku", _vision_sku
                                )
                                conv.pending_product_sku = _vision_sku
                            except Exception as _exc:
                                logger.error("Vision SKU pin error: %s", _exc)
                            logger.info(
                                "Vision-match pin: conv=%s SKU=%s", conv.id, _vision_sku
                            )
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
            # ── Phase 2 wall: no generate_reply() in order stages ─────────────
            # Audio cannot advance the slot machine (user_text = "[voice note]"),
            # so in order stages we re-ask the current slot from a template.
            _audio_order_stages = {
                "order_collection", "awaiting_final_confirmation",
                "payment", "completed",
            }
            if stage in _audio_order_stages:
                from app.services.language_templates import get_template as _get_tpl_au
                _au_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _au_prod = (
                    getattr(pinned_product, "name", None)
                    or getattr(conv, "pending_product_sku", None)
                    or "the product"
                )
                if _next_slot:
                    _au_sq = _build_slot_question(
                        _next_slot,
                        conv=conv,
                        variant_info=variant_info,
                        lang=_au_lang,
                        customer_profile=customer_profile,
                        accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                        available_stock=available_stock,
                        product_name=_au_prod,
                        declined_saved_address=_declined_saved_address,
                    )
                    ai_reply = f"🎤 {_au_sq}" if _au_sq else "🎤"
                else:
                    ai_reply = _get_tpl_au(_au_lang, "already_confirmed")
                logger.info(
                    "conv=%s stage=%r audio in order stage → template (no generate_reply)",
                    conv.id, stage,
                )
            else:
                ai_reply = await gemini_service.generate_reply(
                    effective_text,
                    history=history_dicts,
                    system_prompt=system_prompt,
                    catalogue_context=catalogue_context,
                    language=language,
                )
                _llm_usage = gemini_service.get_last_usage()
            original_type = "audio"
        else:
            # ── Part 1: Deterministic reply for order/payment/completed stages ──
            # For these stages, AI is used ONLY for extraction/classification (done
            # above). The customer-facing reply is ALWAYS built from templates — no
            # generate_reply() call. This eliminates all AI hallucination in the
            # order flow.
            _order_stages = {
                "order_collection", "awaiting_final_confirmation",
                "payment", "completed",
            }
            if stage in _order_stages:
                # Use the established conversation language, not the freshly re-detected one.
                # Single-word payment words like "paid" can be mis-classified; previous_language
                # already encodes the conversation's true language (last_customer_language or "english").
                _tpl_lang = previous_language
                _log_route(conv.id, "TEMPLATE", f"order_stage_{stage}", extra=f"lang={_tpl_lang}")

                # ── DOC B dispatch: (stage, intent) → (next_stage, action) ──────
                # Determine effective intent for the transition table.
                # Synthetic labels collapse the per-stage ad-hoc branches into the
                # single canonical table defined in order_state_machine.py.
                if stage == "completed":
                    # Contextual: action depends on which stage preceded this turn.
                    if conv.current_stage == "payment":
                        _render_action = "confirm_paid_upi"
                    elif conv.current_stage == "awaiting_final_confirmation":
                        _render_action = (
                            "confirm_paid_cod"
                            if (getattr(conv, "payment_method", None) or "COD") == "COD"
                            else "confirm_paid_upi"
                        )
                    else:
                        _render_action = "already_confirmed"
                elif stage == "payment":
                    # FIX 2/4: intercept side-questions during payment wait.
                    # Answer from deterministic facts then re-send payment prompt.
                    if message.type == "text" and _is_order_aside_question(user_text):
                        _pq_lang = previous_language
                        _pq_dt = get_delivery_time_str(pinned_product, client) or "3–7 business days"
                        _pq_answer = ""

                        # FIX 4: cross-product question during payment — try a confident
                        # named-product catalogue match FIRST. _build_order_aside_answer's
                        # generic "price"/"total" branches always answer with the pinned
                        # product, so they must only run as a fallback or this never fires.
                        if client:
                            try:
                                _pq_all = await catalogue_service.list_products(db, client.id)
                                _pq_scored = catalogue_service.search_products_with_scores(
                                    _pq_all, user_text, top_k=3
                                )
                                _pq_pinned = getattr(conv, "pending_product_sku", None)
                                for _pq_sc, _pq_cp in _pq_scored:
                                    if _pq_sc >= 3 and getattr(_pq_cp, "sku", None) != _pq_pinned:
                                        _pq_answer = (
                                            f"{_pq_cp.name} — ₹{int(getattr(_pq_cp, 'price', 0) or 0):,}."
                                        )
                                        break
                            except Exception as _pqe:
                                logger.warning("FIX4 payment cross-product lookup failed: %s", _pqe)

                        # Issue A: KB takes priority over the deterministic fact table.
                        if not _pq_answer and client:
                            try:
                                from app.services import knowledge_service as _pq_kb
                                _pq_kb_entries = await _pq_kb.search_knowledge(
                                    client_id=client.id, query=user_text, db=db,
                                )
                                if _pq_kb_entries:
                                    _pq_answer = _pq_kb_entries[0].answer
                                    try:
                                        for _e in _pq_kb_entries:
                                            _e.usage_count += 1
                                        await db.commit()
                                    except Exception:
                                        pass
                            except Exception as _pq_kb_exc:
                                logger.warning("FIX2 payment KB search failed: %s", _pq_kb_exc)

                        if not _pq_answer:
                            _pq_answer = _build_order_aside_answer(
                                user_text, conv, client, pinned_product, _pq_dt
                            )
                        if _pq_answer:
                            _pq_prod_name = getattr(pinned_product, "name", "the product") or "the product"
                            _pq_qty = getattr(conv, "pending_order_quantity", 1) or 1
                            _pq_suffix = (
                                f"\n\nYour current order: {_pq_prod_name} x{_pq_qty}. "
                                "Reply 'paid' once done. ✅"
                            )
                            _pq_reply = _pq_answer + _pq_suffix
                            logger.info(
                                "Question at order stage → answered from KB/LLM, re-prompting conv=%s",
                                conv.id,
                            )
                            logger.info(
                                "FIX2 payment aside-question: conv=%s — answered, no state change",
                                conv.id,
                            )
                            try:
                                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                                await conversation_service.save_message(db, conv.id, "assistant", _pq_reply)
                            except Exception:
                                pass
                            try:
                                await whatsapp_service.send_text_message(sender_phone, _pq_reply)
                            except Exception:
                                pass
                            try:
                                await _record_usage(db, client)
                            except Exception:
                                pass
                            return {"status": "ok"}

                    # Payment stage always re-shows UPI instructions until PAID.
                    # (PAID detection is an early-return above; we only reach here
                    # when the customer sent something other than a payment word.)
                    _render_action = "reask_payment"
                elif (
                    stage == "awaiting_final_confirmation"
                    and _next_slot is None
                    and message.type == "text"
                    and _is_order_aside_question(user_text)
                ):
                    # ── Issue A: side-question at the summary/confirm step ──────
                    # detect_stage() keeps the customer in awaiting_final_confirmation
                    # for anything that isn't yes/no/change (see conversation_flow.
                    # detect_stage), but until now that fell straight through to the
                    # SLOTS_DONE→show_summary transition below, silently re-dumping
                    # the summary over an unanswered question (e.g. "Can I get
                    # delivery in delhi?" with a KB entry that was never surfaced).
                    # Answer from KB (preferred) or a cheap-model fallback, then
                    # re-show the pending confirm prompt — never advance state.
                    _afc_q_answer = ""
                    if client:
                        try:
                            from app.services import knowledge_service as _afc_kb
                            _afc_kb_entries = await _afc_kb.search_knowledge(
                                client_id=client.id, query=user_text, db=db,
                            )
                            if _afc_kb_entries:
                                _afc_q_answer = _afc_kb_entries[0].answer
                                try:
                                    for _e in _afc_kb_entries:
                                        _e.usage_count += 1
                                    await db.commit()
                                except Exception:
                                    pass
                        except Exception as _afc_kb_exc:
                            logger.warning("AFC KB search failed: %s", _afc_kb_exc)
                    if not _afc_q_answer:
                        _afc_dt = get_delivery_time_str(pinned_product, client) or "3–7 business days"
                        _afc_q_answer = _build_order_aside_answer(user_text, conv, client, pinned_product, _afc_dt)
                    if not _afc_q_answer:
                        try:
                            from openai import AsyncOpenAI as _AfcOpenAI
                            _afc_settings = get_settings()
                            _afc_client_llm = _AfcOpenAI(
                                api_key=_afc_settings.groq_api_key,
                                base_url="https://api.groq.com/openai/v1",
                                max_retries=0,
                            )
                            _afc_resp = await _afc_client_llm.chat.completions.create(
                                model="llama-3.1-8b-instant",
                                messages=[{
                                    "role": "user",
                                    "content": (
                                        f"A customer asked: '{user_text}'. Answer in one short, "
                                        "friendly sentence using only general retail knowledge "
                                        "(no specific prices/policies you don't know). If you "
                                        "cannot answer confidently, say you'll check and get back."
                                    ),
                                }],
                                max_tokens=60,
                                temperature=0.3,
                            )
                            _afc_q_answer = (_afc_resp.choices[0].message.content or "").strip()
                        except Exception as _afc_llm_exc:
                            logger.warning("AFC cheap-model fallback failed: %s", _afc_llm_exc)
                            _afc_q_answer = "Let me check that for you."
                    try:
                        _afc_summary = await _render_order_reply(
                            action="show_summary",
                            conv=conv, db=db, client=client,
                            next_slot=_next_slot, variant_info=variant_info,
                            customer_profile=customer_profile,
                            available_stock=available_stock,
                            declined_saved_address=_declined_saved_address,
                            lang=_tpl_lang, is_first_slot=_is_first_slot,
                        )
                    except RenderError as _afc_re:
                        logger.error(
                            "AFC question re-show summary RenderError conv=%s: %s", conv.id, _afc_re,
                        )
                        _afc_summary = ""
                    ai_reply = f"{_afc_q_answer}\n\n{_afc_summary}" if _afc_summary else _afc_q_answer
                    logger.info(
                        "Question at order stage → answered from KB/LLM, re-prompting conv=%s",
                        conv.id,
                    )
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", ai_reply)
                    except Exception:
                        pass
                    try:
                        await whatsapp_service.send_text_message(sender_phone, ai_reply)
                    except Exception:
                        pass
                    try:
                        await _record_usage(db, client)
                    except Exception:
                        pass
                    return {"status": "ok"}
                else:
                    # order_collection / awaiting_final_confirmation:
                    # resolve action from the transition table.
                    _eff_intent: str
                    if _classified_intent in ("CANCEL", "DISCOUNT_QUERY", "OFF_TOPIC",
                                              "NEW_PRODUCT", "NEW_PRODUCT_OOS"):
                        _eff_intent = _classified_intent
                    elif _next_slot is None:
                        # All slots filled → synthetic SLOTS_DONE triggers summary.
                        _eff_intent = "SLOTS_DONE"
                    else:
                        # ANSWER / OTHER / AUTO_SWITCH_DONE / switch-resolved / etc.
                        _eff_intent = "ANSWER"
                    _, _render_action = order_state_machine.resolve_transition(stage, _eff_intent)

                # Extract OOS product name from intent_override when needed.
                _oos_nm = (
                    _intent_override[4:]
                    if (_intent_override and _intent_override.startswith("OOS:"))
                    else None
                )

                # ── Single render call → customer text ────────────────────────
                try:
                    ai_reply = await _render_order_reply(
                        action=_render_action,
                        conv=conv,
                        db=db,
                        client=client,
                        next_slot=_next_slot,
                        variant_info=variant_info,
                        customer_profile=customer_profile,
                        available_stock=available_stock,
                        declined_saved_address=_declined_saved_address,
                        lang=_tpl_lang,
                        is_first_slot=_is_first_slot,
                        oos_product_name=_oos_nm,
                    )
                except RenderError as _re:
                    logger.error(
                        "RenderError conv=%s stage=%r action=%r — refusing to send: %s",
                        conv.id, stage, _render_action, _re,
                    )
                    return {"status": "render_error"}

                # ── Mark summary_shown after show_summary render ──────────────
                if _render_action == "show_summary" and not getattr(conv, "summary_shown", False):
                    try:
                        await conversation_service.update_order_field(db, conv.id, "summary_shown", True)
                        conv.summary_shown = True
                    except Exception as exc:
                        logger.error("summary_shown update error: %s", exc)

            else:
                # ── Known-fact: delivery time → template (no LLM) ───────────────
                # Short delivery-time queries ("delivery kitne din?", "how long?")
                # have exactly one correct answer from DB — no 70B needed.
                # Must run BEFORE FIX1 so it fires for any browsing stage regardless
                # of whether a product is pinned.
                _DELIVERY_QUERY_KW = (
                    "delivery", "deliver", "kitne din", "kab milega", "kab ayega",
                    "kab aayega", "days", "shipping", "dispatch", "kab pahunchega",
                    "when will", "how long", "kem divas", "kyare malse", "kyare aavse",
                    "time lagega", "kitne dino mein",
                )
                _is_delivery_query = (
                    len(user_text.split()) <= 8
                    and any(kw in user_text.lower() for kw in _DELIVERY_QUERY_KW)
                )
                # FIX 1 (extended): Deterministic compact pinned-product reply.
                # EXTENDED: fires for ALL browsing stages (previously only product_inquiry)
                # so "price?" / "available?" in qualification/objection/offer stages
                # also get a template reply, not a 70B call.
                _pinned_for_reply = pinned_product if getattr(conv, "pending_product_sku", None) else None
                # Fire deterministic compact reply when pinned product exists in any
                # browsing stage AND:
                #   (a) the query matches the pinned product (score > 0), OR
                #   (b) the query is a generic availability/price/delivery question.
                # Do NOT fire when user names a different product (score=0 + specific noun):
                #   "kurti" with only Georgette pinned → falls through to LLM + phantom guard.
                _GENERIC_AVAIL_KW = {
                    "available", "availability", "milega", "stock", "price", "kitna",
                    "is it", "kya", "hai kya", "batao", "bataiye", "iska", "yeh",
                }
                _pinned_relevant = False
                if _pinned_for_reply:
                    _relevance_scores = catalogue_service.search_products_with_scores(
                        [_pinned_for_reply], user_text, top_k=1
                    )
                    _pinned_relevant = bool(_relevance_scores) and _relevance_scores[0][0] > 0
                _is_generic_avail = (
                    len(user_text.split()) <= 7
                    and any(kw in user_text.lower() for kw in _GENERIC_AVAIL_KW)
                )
                if _is_delivery_query:
                    # Route: TEMPLATE — delivery time is a single correct fact from DB.
                    _dt_str = get_delivery_time_str(pinned_product, client) or "3–7 business days"
                    _dt_lang = getattr(conv, "last_customer_language", None) or language or "english"
                    from app.services.language_templates import get_template as _get_dt_tpl
                    ai_reply = _get_dt_tpl(_dt_lang, "delivery_info", delivery_time=_dt_str)
                    if _pinned_for_reply:
                        _order_cta = {
                            "english": "Want to order?",
                            "hindi_roman": "Order karein?",
                            "hinglish": "Order karein?",
                            "hindi_devanagari": "Order karein?",
                            "gujarati_roman": "Order karvo chhe?",
                            "gujarati_script": "Order karvo chhe?",
                        }.get(_dt_lang, "Want to order?")
                        ai_reply = f"{ai_reply} {_order_cta}"
                    _log_route(conv.id, "TEMPLATE", "delivery_query", extra=f"lang={_dt_lang}")
                    logger.info(
                        "conv=%s delivery-query template — no LLM call (delivery=%r)",
                        conv.id, _dt_str,
                    )
                elif _pinned_for_reply and stage in _BROWSING_STAGES_GATE and (
                    _pinned_relevant or _is_generic_avail or _pick_just_resolved
                ):
                    # Route: TEMPLATE — pinned product known-fact query in any browsing stage.
                    # P0-4: deciding this BEFORE the LLM call (not after) is what makes this
                    # a pre-check — when the message is clearly about the pinned product or a
                    # generic availability/price question, the compact template always wins
                    # and the LLM is never invoked, so there is nothing to discard afterward.
                    # NOTE: this must stay scoped to _pinned_relevant/_is_generic_avail — widening
                    # it to fire unconditionally breaks the absent-product path (e.g. "kurti" when
                    # only a Georgette product is pinned must honestly say "not found", not show
                    # the pinned product's card). Those messages still fall through to the LLM +
                    # phantom-guard branch below, where the post-hoc safety net remains as
                    # defense-in-depth — paying for that call is unavoidable since the LLM is what
                    # determines whether the named product is even in the catalogue.
                    _pn2 = _pinned_for_reply.name or conv.pending_product_sku
                    _psku2 = getattr(_pinned_for_reply, "sku", None) or conv.pending_product_sku
                    _av_colors2 = variant_info.get("available_colors", [])
                    _av_sizes2 = variant_info.get("available_sizes", [])
                    _lines2 = [
                        f"{_pn2} [{_psku2}] — {format_price(getattr(_pinned_for_reply, 'price', 0) or 0)}",
                    ]
                    if _av_colors2:
                        _lines2.append(f"Available colors: {', '.join(_av_colors2)}")
                    if _av_sizes2:
                        _lines2.append(f"Available sizes: {', '.join(_av_sizes2)}")
                    _lines2.append("")
                    _lines2.append("Would you like to order? (Yes / No)")
                    ai_reply = "\n".join(_lines2)
                    _log_route(conv.id, "TEMPLATE", "fix1_pinned_known_fact", extra=f"sku={_psku2} stage={stage}")
                    logger.info(
                        "conv=%s FIX1 deterministic compact product reply for %r (stage=%r) — no LLM call",
                        conv.id, _psku2, stage,
                    )
                    # BUG 2 FIX: this is a SINGLE-product offer — clear any stale
                    # multi-choice list so a "Yes" here proceeds to order instead of
                    # being rejected against an old 5-option list ("Multi-choice open").
                    if getattr(conv, "pending_choice_skus", None):
                        try:
                            await conversation_service.set_pending_choice_skus(db, conv.id, None)
                            conv.pending_choice_skus = None
                        except Exception as _pcs_clr_exc:
                            logger.error("BUG2 pending_choice_skus clear failed: %s", _pcs_clr_exc)
                elif getattr(conv, "pending_choice_skus", None) and len(_canonical_browse_products) >= 2:
                    # Route: TEMPLATE — catalog name-match already resolved 2-3 concrete
                    # SKUs (see pending_choice_skus block above). The 70B model would only
                    # reformat rows we already have — match COUNT (not match_score) gates
                    # this, since a multi-match is unambiguous regardless of score.
                    _ml_lines = ["Please reply with the number of your choice:"]
                    for _ml_idx, _ml_prod in enumerate(_canonical_browse_products, 1):
                        _ml_lines.append(
                            f"{_ml_idx}. {_ml_prod.name} [{getattr(_ml_prod, 'sku', None)}] — "
                            f"{format_price(getattr(_ml_prod, 'price', 0) or 0)}"
                        )
                    ai_reply = "\n".join(_ml_lines)
                    _log_route(
                        conv.id, "TEMPLATE", "catalog_multi_template",
                        extra=f"skus={getattr(conv, 'pending_choice_skus', None)} stage={stage}",
                    )
                    logger.info(
                        "conv=%s catalog multi-match template (%d options) — no LLM call",
                        conv.id, len(_canonical_browse_products),
                    )
                    logger.info(
                        "Multi-match template fired conv=%s count=%d (no LLM)",
                        conv.id, len(_canonical_browse_products),
                    )
                else:
                    # Route: LLM or soft-cap template — open browsing question.
                    if _llm_budget == "soft":
                        # Soft LLM cap: skip 70B call, return deterministic catalogue link
                        _soft_biz = (getattr(client, "business_name", None) or "our store") if client else "our store"
                        _soft_slug = getattr(client, "catalogue_slug", None) if client else None
                        _soft_url = f"{settings.catalogue_base_url}/{_soft_slug}" if _soft_slug else settings.catalogue_base_url
                        ai_reply = f"I can help you with orders from {_soft_biz}! Browse our collection: {_soft_url}"
                        _log_route(conv.id, "TEMPLATE", "soft_llm_cap", extra=f"calls={_llm_calls_today}")
                        logger.info("Soft LLM cap: conv=%s calls_today=%d — template reply, skipping 70B.", conv.id, _llm_calls_today)
                    else:
                        # The 70B model is genuinely needed here: no single correct reply exists.
                        # It is used as an UNDERSTANDER only (Section 1): it returns structured
                        # JSON (intent/sku/slots), never the customer-facing text. render_reply()
                        # is the single place that turns this into text, reading the resolved
                        # product fresh from the DB — this is also the fix for the captured
                        # wrong-product bug, since last_shown_sku is now updated here too,
                        # whenever the LLM resolves ANY product (not just the deterministic card).
                        # reason distinguishes a genuinely vague query (no name-match at all —
                        # the only case this call should ever be hit for) from a named-but-
                        # irrelevant-to-pinned-product query, which still needs the LLM to
                        # confirm whether that name exists in the catalogue at all.
                        _ob_reason = "open_browsing_no_match" if _name_match_count == 0 else "open_browsing"
                        _log_route(conv.id, "LLM", _ob_reason, extra=f"stage={stage} model=llama-3.3-70b-versatile")
                        from app.services import llm_intent as _llm_intent, render_reply as _render_reply

                        _intent_result = await _llm_intent.classify_turn(
                            user_text,
                            history=history_dicts,
                            system_prompt=system_prompt,
                            catalogue_context=catalogue_context,
                            language=language,
                        )
                        _llm_usage = gemini_service.get_last_usage()
                        _llm_called_this_turn = True

                        if _intent_result.skus and client:
                            # Multi-product browse/list query ("saree dikhao") — render
                            # straight from DB rows, never a single pin to write here.
                            _listed_products = []
                            for _s in _intent_result.skus:
                                try:
                                    _p = await catalogue_service.find_product_by_sku(db, client.id, _s)
                                except Exception as exc:
                                    logger.error("open_browsing list-sku resolve error: %s", exc)
                                    _p = None
                                if _p is not None:
                                    _listed_products.append(_p)
                            if _listed_products:
                                ai_reply = _render_reply.render_product_list_reply(_listed_products)
                            else:
                                _biz_name = (getattr(client, "business_name", None) or "our store") if client else "our store"
                                ai_reply = _render_reply.render_open_browsing_reply(
                                    _intent_result, pinned_product, variant_info, client, _biz_name, user_text
                                )
                        else:
                            _resolved_product = pinned_product
                            if _intent_result.sku and _intent_result.sku != getattr(pinned_product, "sku", None) and client:
                                try:
                                    _resolved_product = await catalogue_service.find_product_by_sku(
                                        db, client.id, _intent_result.sku
                                    )
                                except Exception as exc:
                                    logger.error("open_browsing sku resolve error: %s", exc)
                                    _resolved_product = None

                            _resolved_variant_info = variant_info
                            if _resolved_product is not None and _resolved_product is not pinned_product:
                                try:
                                    _resolved_variant_info = await catalogue_service.get_product_variant_info(
                                        db, _resolved_product
                                    )
                                except Exception as exc:
                                    logger.error("open_browsing variant_info error: %s", exc)
                                    _resolved_variant_info = {}

                            _biz_name = (getattr(client, "business_name", None) or "our store") if client else "our store"
                            ai_reply = _render_reply.render_open_browsing_reply(
                                _intent_result, _resolved_product, _resolved_variant_info, client, _biz_name, user_text
                            )

                            # Section 3 single-writer: every product surfaced here updates
                            # last_shown_sku, so a later bare "yes" repins THIS product —
                            # never a stale SKU from an earlier, already-completed order.
                            if _resolved_product is not None:
                                try:
                                    await conversation_service.set_last_shown_sku(db, conv.id, _resolved_product.sku)
                                    conv.last_shown_sku = _resolved_product.sku
                                except Exception as exc:
                                    logger.error("last_shown_sku write error (open_browsing): %s", exc)
                # FIX 1: Strip phantom SKUs/prices hallucinated by the LLM (including
                # 429 fallback models that hallucinate more).  Run unconditionally on
                # every browsing-stage reply whenever we have a canonical product set.
                # Pass query=user_text so the guard can detect a no-match and return
                # an honest "not found" instead of listing an irrelevant product.
                if _canonical_browse_products:
                    ai_reply = catalogue_service.guard_product_reply(
                        ai_reply, _canonical_browse_products, query=user_text
                    )
    except Exception as exc:
        logger.error("AI processing error: %s", exc)
        _busy_msg = "Sorry, I'm a bit busy right now — please try again in a moment, or contact us directly."
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _busy_msg)
        except Exception:
            pass
        try:
            await whatsapp_service.send_text_message(sender_phone, _busy_msg)
        except Exception:
            pass
        return {"status": "ok"}

    # Safety net: AI must never emit transactional output (order confirmations,
    # address/name collection, payment instructions) while in any browsing stage.
    # Covers all legacy stages — product_inquiry, qualification, offer_making,
    # objection_handling, greeting — not just product_inquiry (old guard).
    # Anything that slips through the prompt FORBIDDEN blocks is caught here.
    _BROWSING_SAFETY_STAGES = frozenset({
        "greeting", "product_inquiry", "qualification",
        "objection_handling", "offer_making",
    })
    if stage in _BROWSING_SAFETY_STAGES:
        _reply_lower_safety = (ai_reply or "").lower()
        # Transactional phrases that must never appear in browsing-stage replies
        _TRANSACTIONAL_BLOCKED: tuple[str, ...] = (
            # Order confirmation language
            "order placed", "order confirmed", "order ho gaya",
            "your order has been", "order has been placed",
            "order successfully", "order create", "placed your order",
            # Address / name collection
            "delivery address", "what is your address", "address kya hai",
            "address shu chhe", "aapka address", "apna address",
            "may i have your name", "aapka naam kya", "your name please",
            "naam kya hai", "please provide your name", "tamaru naam",
            # Delivery address phrasing that must never appear in a product offer
            "deliver to", "ship to", "delivered to",
            # Payment instructions
            "upi id:", "please pay ₹", "please pay rs",
            "scan this qr", "scan to pay", "reply paid",
            "gpay kar do", "phonepe kar do", "payment karein",
            # Quantity collection (must go to order_collection first)
            "how many pieces", "kitne pieces chahiye", "ketla pieces",
            # Order summary
            "order summary", "━━━━━",  # summary divider character
        )
        if any(phrase in _reply_lower_safety for phrase in _TRANSACTIONAL_BLOCKED):
            logger.warning(
                "Browsing-stage transactional output blocked: stage=%r conv=%s — replacing reply.",
                stage, conv.id,
            )
            # FIX 1+2: If a product is already pinned, build a deterministic
            # availability reply instead of the generic "which item?" deflect.
            # This covers: "Georgette Party Wear is available?" where the AI leaks
            # "deliver to" phrasing — the customer deserves a real answer, not a dead-end.
            if pinned_product and getattr(conv, "pending_product_sku", None):
                _pn = pinned_product.name or conv.pending_product_sku
                _psku = getattr(pinned_product, "sku", None) or conv.pending_product_sku
                _pp = int(getattr(pinned_product, "price", 0) or 0)
                _av_colors = variant_info.get("available_colors", [])
                _av_sizes = variant_info.get("available_sizes", [])
                _color_part = f" Available in {', '.join(_av_colors)}." if _av_colors else ""
                _size_part = f" Sizes: {', '.join(_av_sizes)}." if _av_sizes else ""
                ai_reply = (
                    f"{_pn} [{_psku}] — ₹{_pp:,} is available.{_color_part}{_size_part}"
                    "\n\nWould you like to order? (Yes / No)"
                )
                logger.info(
                    "Browsing-stage guard: pinned product %r → deterministic availability reply (conv=%s)",
                    _psku, conv.id,
                )
            else:
                _biz = (getattr(client, "business_name", None) or "our store") if client else "our store"
                ai_reply = (
                    f"I'd be happy to help you find the right product from {_biz}! "
                    "Which item are you interested in?"
                )
        else:
            # Dynamic guard: saved address must not appear verbatim in browsing replies.
            # Handles the case where the LLM volunteers a "ship to 702 Somerset…" clause
            # that doesn't match the static keyword list above.
            _saved_addr_check = (
                getattr(customer_profile, "address", None)
                if customer_profile else None
            )
            if _saved_addr_check and len(_saved_addr_check) >= 5:
                _addr_lower = _saved_addr_check.lower()
                if _addr_lower in _reply_lower_safety:
                    logger.warning(
                        "Browsing-stage address leak blocked: stage=%r conv=%s — saved address found in reply.",
                        stage, conv.id,
                    )
                    # FIX 1+2: same context-aware fallback for address-leak guard
                    if pinned_product and getattr(conv, "pending_product_sku", None):
                        _pn = pinned_product.name or conv.pending_product_sku
                        _psku = getattr(pinned_product, "sku", None) or conv.pending_product_sku
                        _pp = int(getattr(pinned_product, "price", 0) or 0)
                        _av_colors = variant_info.get("available_colors", [])
                        _av_sizes = variant_info.get("available_sizes", [])
                        _color_part = f" Available in {', '.join(_av_colors)}." if _av_colors else ""
                        _size_part = f" Sizes: {', '.join(_av_sizes)}." if _av_sizes else ""
                        ai_reply = (
                            f"{_pn} [{_psku}] — ₹{_pp:,} is available.{_color_part}{_size_part}"
                            " Reply Yes to order, or No to keep browsing."
                            " Would you like to order?"
                        )
                    else:
                        _biz = (getattr(client, "business_name", None) or "our store") if client else "our store"
                        ai_reply = (
                            f"I'd be happy to help you find the right product from {_biz}! "
                            "Which item are you interested in?"
                        )

    saved_user_content = transcribed_text if transcribed_text else user_text
    try:
        await conversation_service.save_message(db, conv.id, "user", saved_user_content, original_type=original_type, wamid=wamid)
        if transcribed_text:
            ai_reply = f"_{transcribed_text}_\n\n{ai_reply}"
        if _llm_usage:
            await conversation_service.save_message(
                db, conv.id, "assistant", ai_reply,
                path="LLM", model=_llm_usage["model"],
                in_tok=_llm_usage["in_tok"], out_tok=_llm_usage["out_tok"],
            )
        else:
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

    # summary_shown is marked inline in Part 1 when the summary template is rendered.
    # For non-order stages or AI-generated replies (product_inquiry), also mark
    # it if the reply happens to contain "Order Summary" (legacy AI path safety net).
    if (
        stage not in ("order_collection", "awaiting_final_confirmation",
                      "payment", "completed")
        and not getattr(conv, "summary_shown", False)
        and "Order Summary" in (ai_reply or "")
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

    # Improvement 3: increment per-phone daily LLM counter once per turn
    if _llm_called_this_turn:
        try:
            _new_llm_today = (conv.llm_calls_today or 0) + 1
            conv.llm_calls_today = _new_llm_today
            await conversation_service.update_order_field(db, conv.id, "llm_calls_today", _new_llm_today)
            if _new_llm_today >= _llm_soft_cap:
                logger.warning(
                    "LLM daily budget: phone=%s conv=%s calls=%d soft=%d hard=%d status=%s",
                    sender_phone, conv.id, _new_llm_today, _llm_soft_cap, _llm_hard_cap,
                    "soft" if _new_llm_today < _llm_hard_cap else "hard",
                )
        except Exception as _llmce:
            logger.error("llm_calls_today increment error: %s", _llmce)

    all_messages = history_dicts + [
        {"role": "user", "content": user_text},
        {"role": "model", "content": ai_reply},
    ]
    try:
        await lead_service.tag_lead(db, sender_phone, conv.id, all_messages)
    except Exception as exc:
        logger.error("Lead tagging error: %s", exc)

    # Default quantity to 1 if somehow not set (e.g. customer skipped the
    # quantity question and went straight to name+address). Never leave it None
    # at order-creation time — that was the source of the "702 from address" bug.
    if (
        stage == "completed"
        and client
        and getattr(conv, "customer_name", None)
        and getattr(conv, "delivery_address", None)
        and not getattr(conv, "pending_order_quantity", None)
    ):
        try:
            await conversation_service.update_order_field(db, conv.id, "pending_order_quantity", 1)
            conv.pending_order_quantity = 1
        except Exception as exc:
            logger.warning("Quantity default-to-1 failed: %s", exc)

    # ── UPI payment confirmed: update pending_payment → confirmed ──────────────
    # When customer says "paid" and stage advances from payment → completed,
    # find the most-recent pending_payment order and mark it confirmed.
    # Stock is deducted HERE (not at order creation) so unpaid orders never
    # reserve inventory.
    #
    # BUG 2 FIX: query filters by status='pending_payment' and orders by
    # created_at DESC so we always resolve the CURRENT cycle's order, never
    # a previously-paid order from an earlier order in the same conversation.
    # If no pending_payment order is found, we log a warning and skip — sending
    # a phantom "Order confirmed!" on a stale order is worse than silence.
    if (
        stage == "completed"
        and _stored_stage == "payment"
        and client
    ):
        from app.models.order import Order as OrderModel
        _pay_confirm_result = await db.execute(
            select(OrderModel)
            .where(
                OrderModel.conversation_id == conv.id,
                OrderModel.status == "pending_payment",
            )
            .order_by(OrderModel.created_at.desc())
            .limit(1)
        )
        _pay_order = _pay_confirm_result.scalar_one_or_none()
        if _pay_order:
            try:
                await order_service.mark_order_paid(db, _pay_order, client)
                logger.info(
                    "UPI payment confirmed via mark_order_paid: order %s conv=%s",
                    _pay_order.order_number, conv.id,
                )
                # Update customer profile stats
                try:
                    await customer_service.record_order(
                        db,
                        client_id=client.id,
                        phone=sender_phone,
                        order_total=_pay_order.total_amount,
                        customer_name=conv.customer_name,
                        delivery_address=conv.delivery_address,
                        payment_method=_pay_order.payment_method,
                    )
                    await db.commit()
                except Exception as exc:
                    logger.warning("Customer stats update on paid failed: %s", exc)
                # BUG 1 FIX: reset conversation to a clean state for the next order cycle.
                # Clears product-specific slots; keeps customer name + address for convenience.
                await _reset_order_slots_after_completion(db, conv.id, conv)
            except Exception as exc:
                logger.error("UPI order confirmation failed for conv=%s: %s", conv.id, exc)
        else:
            # No pending_payment order found — this is a phantom "paid" signal
            # (e.g. after an order was already paid, or no order was ever created).
            # Log a warning and do NOT send a confirmation reply.
            logger.warning(
                "UPI 'paid' received but no pending_payment order found for conv=%s "
                "— phantom confirm blocked.",
                conv.id,
            )

    # Auto-create order when the conversation reaches 'completed' (COD) or
    # 'payment' (UPI pending) stage and all required fields are collected.
    # GUARDRAIL (Phase 1): pending_product_sku must be set — order rows may ONLY
    # originate from the deterministic slot-machine path where a product was
    # explicitly pinned.  This blocks any accidental order creation that could
    # slip through from legacy browsing-stage AI replies.
    _should_create_order = (
        # UPI: customer said yes to summary → create pending_payment order
        (stage == "payment" and _stored_stage == "awaiting_final_confirmation")
        # COD: customer said yes → create confirmed order immediately
        or (stage == "completed" and _stored_stage == "awaiting_final_confirmation")
    )
    if (
        _should_create_order
        and client
        and getattr(conv, "pending_product_sku", None)  # must come through pinned-SKU path
        and getattr(conv, "customer_name", None)
        and getattr(conv, "delivery_address", None)
        and getattr(conv, "pending_order_quantity", None)
    ):
        # BUG 3 FIX: only block creation when a pending_payment order already
        # exists for this conversation (idempotency for duplicate webhooks / rapid
        # double-taps on the same cycle).  A paid/completed order from a prior
        # cycle must NOT prevent a new order row for the next cycle.
        from app.models.order import Order as OrderModel
        existing_check = await db.execute(
            select(OrderModel)
            .where(
                OrderModel.conversation_id == conv.id,
                OrderModel.status == "pending_payment",
            )
            .limit(1)
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

                # ── Stock validation ──────────────────────────────────────
                # Abort order creation if requested quantity exceeds stock.
                # Use the VARIANT-specific stock (not product.stock which is
                # the sum of all variants) so we catch e.g. Black/S having 0
                # units even when the product total is 20.
                if product and conv.pending_order_quantity:
                    if product.has_variants:
                        _sel_c = getattr(conv, "selected_color", None)
                        _sel_s = getattr(conv, "selected_size", None)
                        _sel_m = getattr(conv, "selected_material", None)
                        from app.models.product_variant import ProductVariant as _PVoc
                        _oc_vstmt = select(_PVoc).where(
                            _PVoc.product_id == product.id,
                            _PVoc.is_active == True,  # noqa: E712
                        )
                        if _sel_c:
                            _oc_vstmt = _oc_vstmt.where(_PVoc.color == _sel_c)
                        if _sel_s:
                            _oc_vstmt = _oc_vstmt.where(_PVoc.size == _sel_s)
                        if _sel_m:
                            _oc_vstmt = _oc_vstmt.where(_PVoc.material == _sel_m)
                        _oc_vr = await db.execute(_oc_vstmt)
                        _oc_variant = _oc_vr.scalars().first()
                        _oc_stock = _oc_variant.stock if _oc_variant else 0
                    else:
                        _oc_stock = getattr(product, "stock", None)
                    if _oc_stock is not None and conv.pending_order_quantity > _oc_stock:
                        logger.warning(
                            "Order blocked: requested qty %d > stock %d for product %s",
                            conv.pending_order_quantity, available_stock, product.sku,
                        )
                        # Reset quantity so it can be re-collected correctly next turn
                        try:
                            await conversation_service.update_order_field(
                                db, conv.id, "pending_order_quantity", None
                            )
                            conv.pending_order_quantity = None
                        except Exception:
                            pass
                        # Skip order creation — the AI prompt already instructs the
                        # agent to ask the customer for a reduced quantity.
                        raise ValueError(
                            f"qty_exceeds_stock:{conv.pending_order_quantity}:{available_stock}"
                        )

                # ── Hard completeness guard ───────────────────────────────
                # All required fields must be present before any DB write.
                # Variant fields are required when the product declares them.
                _missing_fields: list[str] = []
                if not conv.customer_name:
                    _missing_fields.append("customer_name")
                if not conv.delivery_address:
                    _missing_fields.append("delivery_address")
                if not conv.pending_order_quantity:
                    _missing_fields.append("pending_order_quantity")
                if not conv.payment_method:
                    _missing_fields.append("payment_method")
                _vi_check = variant_info or {}
                if _vi_check.get("needs_color") and not getattr(conv, "selected_color", None):
                    _missing_fields.append("selected_color")
                if _vi_check.get("needs_size") and not getattr(conv, "selected_size", None):
                    _missing_fields.append("selected_size")
                if _vi_check.get("needs_material") and not getattr(conv, "selected_material", None):
                    _missing_fields.append("selected_material")

                if _missing_fields:
                    logger.error(
                        "Order creation BLOCKED — missing required fields %s for conv=%s. "
                        "Full slot state: color=%r size=%r material=%r qty=%r name=%r "
                        "address=%r payment=%r",
                        _missing_fields, conv.id,
                        getattr(conv, "selected_color", None),
                        getattr(conv, "selected_size", None),
                        getattr(conv, "selected_material", None),
                        getattr(conv, "pending_order_quantity", None),
                        getattr(conv, "customer_name", None),
                        getattr(conv, "delivery_address", None),
                        getattr(conv, "payment_method", None),
                    )
                    # Revert stage so the slot machine re-prompts next turn.
                    stage = "order_collection"
                    try:
                        await conversation_service.update_stage(db, conv.id, "order_collection")
                    except Exception as _se:
                        logger.error("Stage revert after blocked order: %s", _se)
                    raise ValueError(f"order_incomplete:{','.join(_missing_fields)}")

                # Resolve payment method: honour customer's explicit choice if
                # captured; if COD is disabled on this client default to UPI.
                accepts_cod = getattr(client, "accepts_cod", False)
                conv_payment = getattr(conv, "payment_method", None)
                if conv_payment == "COD" and not accepts_cod:
                    # Customer said COD but this seller doesn't accept it;
                    # override silently — agent already explained UPI-only.
                    conv_payment = "UPI"
                resolved_payment = conv_payment or ("COD" if accepts_cod else "UPI")

                # All orders start as pending_payment; mark_order_paid() transitions
                # COD orders to paid immediately after creation.
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
                    variant_color=getattr(conv, "selected_color", None),
                    variant_size=getattr(conv, "selected_size", None),
                    variant_material=getattr(conv, "selected_material", None),
                    idempotency_key=wamid,
                )
                _order_initial_status = created_order.status
                logger.info(
                    "Auto-created order %s (status=%s) from conversation %s",
                    created_order.order_number, _order_initial_status, conv.id,
                )
                cost_log.print_report(conv.id, created_order.order_number)

                # Bank transfer: send details as a separate message (UPI instructions
                # are already embedded in ai_reply; COD needs no payment step).
                if resolved_payment == "bank_transfer":
                    try:
                        await _send_bank_transfer_details(
                            sender_phone=sender_phone,
                            amount_inr=created_order.total_amount,
                            order_number=created_order.order_number,
                            client=client,
                        )
                    except Exception as exc:
                        logger.warning("Bank transfer details send failed: %s", exc)

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

                # COD: transition immediately to paid + deduct stock atomically.
                # UPI: order stays pending_payment until customer confirms payment.
                if resolved_payment != "UPI":
                    try:
                        await order_service.mark_order_paid(db, created_order, client)
                        # BUG 1 FIX: reset slots so order 2 starts from a clean state.
                        await _reset_order_slots_after_completion(db, conv.id, conv)
                    except Exception as exc:
                        logger.warning("mark_order_paid (COD) failed for conv=%s: %s", conv.id, exc)
            except Exception as exc:
                logger.error("Auto-create order failed for conversation %s: %s", conv.id, exc)

    # Choose plain text vs interactive buttons based on stage + reply content.
    # Only WhatsApp supports interactive messages; Instagram always gets text.
    is_whatsapp = getattr(conv, "channel", "whatsapp") == "whatsapp"
    _client_accepts_cod = getattr(client, "accepts_cod", False) if client else False
    button_type = _should_use_buttons(stage, ai_reply, _client_accepts_cod, next_slot=_next_slot) if is_whatsapp else "text"

    # ── E2: interactive buttons/list for product offers and multi-option choices ──
    # Purely additive on top of the text reply already built above — typing
    # "yes"/"1"/a product name still resolves exactly as before; tapping is
    # just a faster path to the same outcome.
    _offer_buttons: list[dict] = []
    _choice_buttons: list[dict] = []
    _choice_list_rows: list[dict] = []
    if is_whatsapp and button_type == "text":
        if (
            "would you like to order?" in ai_reply.lower()
            and pinned_product is not None
            and getattr(conv, "pending_product_sku", None)
        ):
            button_type = "offer_buttons"
            _offer_buttons = [
                {"id": "offer_yes", "title": "Yes"},
                {"id": "offer_no", "title": "No"},
            ]
        else:
            _pcs_for_buttons: list = []
            try:
                import json as _json_btn
                _pcs_raw_btn = getattr(conv, "pending_choice_skus", None)
                if _pcs_raw_btn:
                    _pcs_for_buttons = _json_btn.loads(_pcs_raw_btn)
            except Exception:
                _pcs_for_buttons = []
            if _pcs_for_buttons and client:
                _choice_products = []
                for _cb_sku in _pcs_for_buttons:
                    try:
                        _cb_p = await catalogue_service.find_product_by_sku(db, client.id, _cb_sku)
                    except Exception:
                        _cb_p = None
                    if _cb_p:
                        _choice_products.append(_cb_p)
                if 2 <= len(_choice_products) <= 3:
                    # WhatsApp allows max 3 buttons — one per product, id=SKU.
                    button_type = "choice_buttons"
                    for _cb_p in _choice_products:
                        _choice_buttons.append({
                            "id": _cb_p.sku,
                            "title": (_cb_p.name or _cb_p.sku)[:20],
                        })
                elif len(_choice_products) >= 4:
                    # 4+ options: WhatsApp buttons cap at 3 — use a list message instead.
                    button_type = "choice_list"
                    for _cb_p in _choice_products[:10]:
                        _choice_list_rows.append({
                            "id": _cb_p.sku,
                            "title": (_cb_p.name or _cb_p.sku)[:24],
                            "description": format_price(getattr(_cb_p, "price", 0) or 0),
                        })

    # phone_number_id: prefer the one in the webhook metadata (most accurate),
    # fall back to the client's configured ID.
    pid = webhook_phone_number_id or (
        getattr(client, "whatsapp_phone_number_id", None) if client else None
    )

    # Hard safety net: the "Order confirmed!" message must NEVER carry interactive
    # buttons regardless of what happened inside the order creation block.
    # If the guard or an exception inside that block reverted stage to something
    # other than "completed" but ai_reply still contains "confirm", _should_use_buttons
    # might return confirm_buttons.  We override that here for the completed case.
    if stage == "completed":
        button_type = "text"
    # Payment stage: show "I've Paid" button alongside the UPI instructions.
    # "paid_done" maps to "paid" in the interactive parser (already wired).
    elif stage == "payment" and is_whatsapp and pid:
        button_type = "paid_button"

    logger.info(
        "CONV-TRANSCRIPT conv=%s\n%s",
        conv.id,
        "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in (history_dicts + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": ai_reply},
            ])
        ),
    )

    # BUG 4 FIX: generate a fresh nonce for the buttons about to be sent.
    # Encode it into every interactive button ID so the server can validate it
    # on the next tap.  Only needed when we are actually sending buttons.
    _send_nonce: str | None = None
    # BUG 3 FIX: offer/choice buttons were never nonce-encoded, so a tap on an
    # old "Yes/No" offer or an old multi-choice option always processed even
    # after the conversation moved past that stage. Cover them the same way
    # as confirm/paid/payment buttons.
    _sends_buttons = button_type in (
        "confirm_buttons", "paid_button", "payment_buttons",
        "offer_buttons", "choice_buttons", "choice_list",
    )
    if _sends_buttons and is_whatsapp and pid:
        _send_nonce = await _rotate_nonce(db, conv.id, conv)

    def _nb(action: str) -> str:
        """Return nonce-encoded button id, or bare action when nonce unavailable."""
        if _send_nonce:
            return _encode_btn(action, conv.id, _send_nonce)
        return action

    try:
        if button_type == "payment_buttons" and pid:
            # Build button list based on enabled payment methods for this client/order
            _order_total_for_buttons = 0.0
            if conv and pinned_product:
                _qty = getattr(conv, "pending_order_quantity", None) or 0
                _order_total_for_buttons = _qty * (getattr(pinned_product, "price", 0) or 0)

            _accepts_upi = getattr(client, "accepts_upi", True) if client else True
            _upi_id_set = bool(getattr(client, "upi_id", None)) if client else False
            _cod_limit = getattr(client, "cod_limit", None) if client else None
            _accepts_bank = getattr(client, "accepts_bank_transfer", False) if client else False
            _bank_ready = bool(
                getattr(client, "bank_account_name", None)
                and getattr(client, "bank_account_number", None)
                and getattr(client, "bank_ifsc", None)
            ) if client else False

            cod_eligible = (
                _client_accepts_cod
                and (_cod_limit is None or _order_total_for_buttons <= _cod_limit)
            )

            payment_buttons = []
            if _accepts_upi and _upi_id_set:
                payment_buttons.append({"id": _nb("upi"), "title": "💳 Pay via UPI"})
            if cod_eligible:
                payment_buttons.append({"id": _nb("cod"), "title": "🚚 Cash on Delivery"})
            if _accepts_bank and _bank_ready:
                payment_buttons.append({"id": _nb("bank_transfer"), "title": "🏦 Bank Transfer"})

            # WhatsApp buttons support max 3; if none configured fall back to text
            payment_buttons = payment_buttons[:3]

            if len(payment_buttons) <= 1:
                # Only one option — no need for a button choice; send AI reply as text
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
            else:
                sent = await whatsapp_service.send_button_message(
                    to_phone_number=sender_phone,
                    body_text=ai_reply,
                    buttons=payment_buttons,
                    phone_number_id=pid,
                )
                if not sent:
                    await whatsapp_service.send_text_message(
                        to_phone_number=sender_phone,
                        message_text=ai_reply,
                    )
        elif button_type == "confirm_buttons" and pid:
            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=[
                    {"id": _nb("confirm_pay"), "title": "Confirm & Pay"},
                    {"id": _nb("cancel_order"), "title": "Cancel"},
                ],
                phone_number_id=pid,
            )
            if not sent:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        elif button_type == "paid_button" and pid:
            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=[{"id": _nb("paid_done"), "title": "I've Paid"}],
                phone_number_id=pid,
            )
            if not sent:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        elif button_type == "offer_buttons" and pid:
            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=[{**b, "id": _nb(b["id"])} for b in _offer_buttons],
                phone_number_id=pid,
            )
            if not sent:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        elif button_type == "choice_buttons" and pid:
            sent = await whatsapp_service.send_button_message(
                to_phone_number=sender_phone,
                body_text=ai_reply,
                buttons=[{**b, "id": _nb(b["id"])} for b in _choice_buttons],
                phone_number_id=pid,
            )
            if not sent:
                await whatsapp_service.send_text_message(
                    to_phone_number=sender_phone,
                    message_text=ai_reply,
                )
        elif button_type == "choice_list" and pid:
            sent = await whatsapp_service.send_list_message(
                to_phone_number=sender_phone,
                header_text="Choose an option",
                body_text=ai_reply,
                button_text="View options",
                sections=[{
                    "title": "Options",
                    "rows": [{**r, "id": _nb(r["id"])} for r in _choice_list_rows],
                }],
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

    # ── Deferred product image sends ──────────────────────────────────────────
    # Images queued during SKU/name-match are sent now — AFTER the text reply —
    # so a failed image send never aborts the pin path or blocks the text.
    for _img_url, _img_caption in _pending_product_images:
        try:
            await whatsapp_service.send_image_message(
                to_phone_number=sender_phone,
                image_url=_img_url,
                caption=_img_caption,
            )
        except Exception as _img_exc:
            logger.error("Product image send error (non-fatal): %s", _img_exc)

    return {"status": "ok"}
