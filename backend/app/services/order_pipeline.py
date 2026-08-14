"""
Channel-neutral WhatsApp/Instagram order-handling pipeline.

This module is being incrementally extracted (strangler-fig) from the
previously fully-inlined logic in `app/routers/webhook.py`'s
`receive_message` handler. The goal is a single channel-neutral entry
point, `handle_inbound_message(ctx) -> PipelineResult`, that contains
all order-flow business logic with no direct calls to any channel's
send API (e.g. gated `outbound.*` sends). Channel-specific adapters
(currently only `app/routers/webhook.py` for WhatsApp) build the neutral
`InboundContext`, call this pipeline, and translate the returned
`PipelineResult` into channel-specific API calls.

SLICE 1 (this commit): pure, no-I/O helper functions and their
supporting constants/regexes moved here verbatim from webhook.py.
`webhook.py` now imports these names back from this module so all
existing call sites continue to work unchanged.
"""

import asyncio
import json as _json
import logging
import random
import re
import re as _re_addr
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.services import billing_service, catalogue_service, conversation_flow, conversation_service, customer_service
from app.services import cost_log, escalation_service, gemini_service, lead_service, order_service, order_state_machine
from app.services import language_service as _lang_svc, usage_service, vision_service, voice_service
from app.services.delivery_service import get_delivery_time_str
from app.services.language_templates import format_price, get_template
from app.services.order_state_machine import RenderError

# NOTE: deliberately uses the webhook router's logger name (not __name__) so
# log lines emitted by helpers moved out of webhook.py during the strangler-fig
# refactor are indistinguishable from pre-refactor output — several
# characterization tests assert on caplog records under logger
# "app.routers.webhook" specifically (e.g. tests/replay/test_characterization_gaps.py).
# Revisit this once all callers have moved off the legacy logger name.
logger = logging.getLogger("app.routers.webhook")


# ---------------------------------------------------------------------------
# Channel-neutral result type (SLICE 2)
# ---------------------------------------------------------------------------
# Early guards (rate limit / dedup / hard LLM cap / stale-paid / nonce /
# cancel-in-payment / blocklist / human-takeover / duplicate confirm+payment)
# previously did `await <channel send>(...); return` inline
# in webhook.py. They are converted here to return a PipelineResult (or None
# when the guard does not fire) so the WhatsApp adapter in webhook.py — and,
# later, an Instagram adapter — can decide how to actually send it.
# ---------------------------------------------------------------------------


@dataclass
class ButtonSpec:
    """One WhatsApp/Instagram quick-reply button: id + display title."""

    id: str
    title: str


@dataclass
class ListOptionSpec:
    """One row in a WhatsApp list message: id + display title (+ optional description)."""

    id: str
    title: str
    description: str | None = None


@dataclass
class PipelineResult:
    """
    Channel-neutral outcome of processing one inbound message.

    SLICE 8: this is also the "SendInstruction" the dispatch layer now
    returns — buttons/list_options carry RAW action ids (e.g. "confirm_pay",
    a product SKU), never WhatsApp's nonce-encoded "{action}~{conv_id}~
    {nonce}" form. Nonce rotation/encoding is a WhatsApp-specific delivery
    concern and stays in the WhatsApp adapter (app/routers/_whatsapp_adapter.py)
    — a future Instagram adapter would translate the same buttons/list_options
    into IG quick-replies (≤3, also id+title) or a numbered text fallback
    (>3 options, or for list_options since IG has no native list UI),
    without needing any nonce concept at all.

    text:         Plain-text reply, or None when nothing should be sent
                  (e.g. silent save during human takeover). Also serves as
                  the body text shown above buttons/list, and as the
                  fallback text the adapter sends if the interactive send
                  fails.
    buttons:      Quick-reply buttons to render, if any.
    list_options: List-message rows to render, if any.
    list_header:  Header text for a list message (WhatsApp-specific render
                  detail, but kept here as plain data — an adapter that
                  doesn't support a header simply ignores it).
    list_button_text: Label on the button that opens the list.
    images:       List of (image_url, caption) tuples to send AFTER the main
                  text/buttons/list — mirrors webhook.py's original deferred
                  product-image queue (sent after the text reply so a failed
                  image send never blocks/aborts the text).
    nonce:        New button nonce to persist, if the adapter needs to know it.
    skip_send:    True when the pipeline already decided nothing should be
                  sent to the customer at all (distinct from text=None with a
                  send still expected to happen, which doesn't occur today).
    pre_texts:    Plain-text message(s) to send BEFORE the main text/buttons/
                  list — mirrors webhook.py's original bank-transfer-details
                  side-send, which fired immediately after order creation,
                  before the main ai_reply was dispatched.
    status:       Value the caller's HTTP handler should report back (e.g.
                  "ok" or "render_error") — lets handle_inbound_message
                  preserve webhook.py's original `{"status": "render_error"}`
                  short-circuit without sending anything.
    """

    text: str | None
    buttons: list[ButtonSpec] | None = None
    list_options: list[ListOptionSpec] | None = None
    list_header: str | None = None
    list_button_text: str | None = None
    images: list[tuple[str, str]] | None = None
    nonce: str | None = None
    skip_send: bool = False
    pre_texts: list[str] | None = None
    status: str = "ok"

# ---------------------------------------------------------------------------
# Button-nonce helpers (BUG 4 FIX)
# ---------------------------------------------------------------------------
# WhatsApp buttons cannot be disabled after sending.  We embed a short nonce
# in every button ID so the server can detect stale / out-of-order taps and
# reject them without any state change.
#
# Encoding:  "{action}~{conv_id}~{nonce}"   (tilde avoids collision with "-")
# Max length: "bank_transfer~999999999~abcd1234" = 32 chars, well under WA 256.
# ---------------------------------------------------------------------------


def _generate_nonce() -> str:
    """Return an 8-char random hex token for button IDs."""
    return secrets.token_hex(4)


def _encode_btn(action: str, conv_id: int, nonce: str) -> str:
    """Encode a button action with conversation id + nonce."""
    return f"{action}~{conv_id}~{nonce}"


def _decode_btn(btn_id: str) -> tuple[str, str, str] | None:
    """
    Parse a nonce-encoded button ID.

    Returns (action, conv_id_str, nonce) or None when the ID is not
    nonce-encoded (legacy button, typed word, etc.).
    """
    if "~" not in btn_id:
        return None
    parts = btn_id.split("~")
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


async def _rotate_nonce(
    db,
    conv_id: int,
    conv,
    new_nonce: str | None = None,
) -> str:
    """
    Generate (or accept) a nonce, persist it to the conversation row, and
    return it.  Errors are swallowed so a nonce write failure never aborts
    message processing.
    """
    nonce = new_nonce or _generate_nonce()
    try:
        from app.services import conversation_service as _cs_nonce
        await _cs_nonce.update_order_field(db, conv_id, "current_button_nonce", nonce)
        conv.current_button_nonce = nonce
    except Exception as exc:
        logger.error("Nonce rotation failed for conv=%s: %s", conv_id, exc)
    return nonce


# Deterministic one-line questions per slot — used when the AI bypasses the slot machine.
_SLOT_FALLBACK_QUESTIONS: dict[str, dict[str, str]] = {
    "quantity": {
        "english": "How many pieces would you like?",
        "hindi": "Kitne pieces chahiye?",
        "hinglish": "Kitne pieces chahiye?",
        "gujarati": "Ketla pieces joiye?",
    },
    "color": {
        "english": "Which color would you like?",
        "hindi": "Kaunsa color chahiye?",
        "hinglish": "Kaunsa color chahiye?",
        "gujarati": "Kayo color joiye?",
    },
    "size": {
        "english": "Which size would you like?",
        "hindi": "Kaunsa size chahiye?",
        "hinglish": "Kaunsa size chahiye?",
        "gujarati": "Kayu size joiye?",
    },
    "material": {
        "english": "Which material would you like?",
        "hindi": "Kaunsa material chahiye?",
        "hinglish": "Kaunsa material chahiye?",
        "gujarati": "Kayu material joiye?",
    },
    "customer_name": {
        "english": "May I have your name please?",
        "hindi": "Aapka naam kya hai?",
        "hinglish": "Aapka naam kya hai?",
        "gujarati": "Tamaru naam shu chhe?",
    },
    "delivery_address": {
        "english": "What is your delivery address?",
        "hindi": "Delivery address kya hai?",
        "hinglish": "Delivery address kya hai?",
        "gujarati": "Delivery address shu chhe?",
    },
    "mobile_number": {
        "english": "What is your mobile number for delivery?",
        "hindi": "Delivery ke liye mobile number kya hai?",
        "hinglish": "Delivery ke liye mobile number kya hai?",
        "gujarati": "Delivery mate mobile number shu chhe?",
    },
    "payment_method": {
        "english": "How would you like to pay — UPI or Cash on Delivery?",
        "hindi": "Aap UPI se denge ya COD?",
        "hinglish": "Aap UPI se denge ya COD?",
        "gujarati": "UPI ke COD — kem bharvu chhe?",
    },
}

# ── Order-stage aside-question detection ─────────────────────────────────────
# Detects customer questions that should be answered inline without advancing state.

_QUESTION_STARTERS = frozenset({
    "how", "what", "why", "when", "where", "which", "who",
    "kya", "kem", "ketla", "kitna", "kitne", "kaisi", "kaisa",
})

_ORDER_QUESTION_KEYWORDS = frozenset({
    "delivery", "deliver", "shipping", "charge", "charges", "fee",
    "price", "total", "amount", "cost",
    "stock", "available", "availability",
    "return", "refund", "exchange",
    "upi", "payment", "bhugtan",
    "kitne din", "kab milega", "kab aayega",
})

# Phrases that indicate a change-address intent (prefix to strip).
_CHANGE_ADDRESS_PATTERNS = (
    r"change\s+(?:it\s+)?to\s+",
    r"change\s+(?:my\s+)?address\s+to\s+",
    r"deliver\s+(?:it\s+)?to\s+",
    r"naya\s+address\s*[:\-]?\s*",
    r"address\s+change\s+(?:karein|karo|kar\s+do|to)?\s*",
    r"new\s+address\s*[:\-]?\s*",
)
_CHANGE_ADDR_RE = _re_addr.compile(
    r"^(?:" + "|".join(_CHANGE_ADDRESS_PATTERNS) + r")",
    _re_addr.IGNORECASE,
)
_CHANGE_ADDR_INTENT_RE = _re_addr.compile(
    r"\b(change\s+(?:my\s+|the\s+)?address|change\s+it\s+to|deliver\s+(?:it\s+)?to|"
    r"naya\s+address|address\s+change|new\s+address)\b",
    _re_addr.IGNORECASE,
)

_ACK_PHRASES = frozenset({
    "ok", "okay", "got it", "i got it", "achha", "acha", "achha got it",
    "fine", "sure", "understood", "theek", "sahi", "noted",
    "alright", "cool", "great", "nice", "samajh gaya", "samajh gaya",
    "no problem", "that's fine", "thats fine",
})


def _is_order_aside_question(text: str) -> bool:
    """True when the message is a side-question during an active order, not a slot answer."""
    lower = text.lower().strip()
    if "?" in lower:
        return True
    first_word = lower.split()[0] if lower.split() else ""
    if first_word in _QUESTION_STARTERS:
        return True
    return any(kw in lower for kw in _ORDER_QUESTION_KEYWORDS)


def _is_availability_question(text: str) -> bool:
    """True for 'is X available?' / 'do you have X?' style stock questions."""
    lower = text.lower().strip()
    if "available" in lower or "availability" in lower or "in stock" in lower or "out of stock" in lower:
        return True
    return lower.startswith("do you have") or lower.startswith("is there")


# Common saree/garment colour words used to spot a colour named in an
# availability question even when it isn't one of this product's variants.
_KNOWN_COLOR_WORDS = frozenset({
    "orange", "blue", "green", "pink", "red", "yellow", "black", "white",
    "purple", "maroon", "beige", "grey", "gray", "brown", "navy", "gold",
    "silver", "cream", "magenta", "turquoise", "teal", "lavender",
})


def _build_availability_answer(
    user_text: str,
    pinned_product,
    variant_info: dict | None,
    available_stock: int | None,
) -> str:
    """
    Answer an availability question ("is orange available?") from the
    CATALOG / current product's variants — never from the KB, since KB
    keyword overlap on a word like "available" can return an unrelated FAQ.

    Returns "" when there isn't enough info to answer confidently (caller
    should fall back to a safe message rather than inventing an answer).
    """
    if pinned_product is None:
        return ""
    lower = user_text.lower()
    prod_name = getattr(pinned_product, "name", None) or "this product"
    vi = variant_info or {}
    colors = vi.get("available_colors", []) or []
    sizes = vi.get("available_sizes", []) or []
    materials = vi.get("available_materials", []) or []

    # Word-boundary match only — a naive substring check let a single-letter
    # size like "L" match inside an unrelated token (e.g. the "L" in SKU
    # "LH10042"), causing a general availability question to be answered as
    # if the customer had named that specific size.
    for options in (colors, sizes, materials):
        for opt in options:
            if opt and re.search(rf"\b{re.escape(opt.lower())}\b", lower):
                return f"Yes, {opt} is available for {prod_name}."

    cleaned = lower.replace("?", " ")
    for word in cleaned.split():
        word = word.strip(".,!")
        if word in _KNOWN_COLOR_WORDS and word not in {c.lower() for c in colors}:
            opts = ", ".join(colors) if colors else "see our catalogue"
            return f"{word.capitalize()} isn't available for {prod_name}. Available colors: {opts}."

    # No specific variant named — general availability answer with full
    # details, not a guess at one arbitrary variant.
    if available_stock is not None:
        if available_stock <= 0:
            return f"Sorry, {prod_name} is currently out of stock."
        return f"Yes, {prod_name} is available."

    if colors or sizes or materials:
        price = getattr(pinned_product, "price", None)
        answer = f"Yes, {prod_name} is available"
        answer += f" — ₹{int(price):,}." if price else "."
        if colors:
            answer += f" Colors: {', '.join(colors)}."
        if sizes:
            answer += f" Sizes: {', '.join(sizes)}."
        if materials:
            answer += f" Materials: {', '.join(materials)}."
        return answer

    return ""


# Explicit new-purchase / product-switch phrasing — deliberately broad (the
# actual gate against false positives is requiring a CONFIDENT catalogue
# match via _find_confident_product_match, not this regex alone; see its
# call sites).
_PURCHASE_INTENT_RE = re.compile(
    r"\b("
    r"i want to (?:buy|order|get|have)|"
    r"i'?d like to (?:buy|order|get)|"
    r"i would like to (?:buy|order|get)|"
    r"change (?:it |the order |my order )?to|"
    r"switch (?:it |the order )?to|"
    r"instead of|"
    r"can i (?:get|have|order|buy)"
    r")\b",
    re.IGNORECASE,
)


def is_purchase_intent(text: str) -> bool:
    """True when *text* expresses explicit new-purchase/switch intent ("I want to buy X", "change to X", "switch to X")."""
    return bool(text) and bool(_PURCHASE_INTENT_RE.search(text))


# Cancel-the-whole-order phrasing at payment stage. Deliberately a plain
# keyword/phrase list (not an LLM classification) — same reasoning as
# _PURCHASE_INTENT_RE: cheap, deterministic, and this is a payment-stage
# guard where a false negative just falls through to the existing reminder
# (safe) while a false positive cancels a real order (must stay low-risk).
# Maintain as a flat constant list; extend with more phrasings as they show
# up in logs rather than trying to cover everything up front.
_CANCEL_INTENT_PATTERNS = (
    "don't want", "dont want", "do not want",
    "cancel", "not interested", "never mind", "nevermind", "stop",
)
_CANCEL_INTENT_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _CANCEL_INTENT_PATTERNS) + r")\b",
    re.IGNORECASE,
)


def is_cancel_intent(text: str) -> bool:
    """True when *text* expresses intent to cancel/abandon the order ("I don't want this", "cancel", "never mind")."""
    return bool(text) and bool(_CANCEL_INTENT_RE.search(text))


async def _find_confident_product_match(db, client, user_text: str, exclude_sku: str | None):
    """
    Return a single, confidently-matched catalogue Product for *user_text*,
    excluding *exclude_sku* (the currently pinned/active product) — or None.

    Shared by the payment-stage cross-product aside-question answer (FIX 4)
    and the mid-payment purchase-intent switch detector so both use the
    identical scored-search matching rather than two independently-tuned
    copies of the same lookup.
    """
    if not client:
        return None
    try:
        _all_products = await catalogue_service.list_products(db, client.id)
        _scored = catalogue_service.search_products_with_scores(_all_products, user_text, top_k=3)
        for _score, _candidate in _scored:
            if _score >= 3 and getattr(_candidate, "sku", None) != exclude_sku:
                return _candidate
    except Exception as exc:
        logger.warning("Confident product match lookup failed: %s", exc)
    return None


# Matches "deliver(y) to/in <place>", "ship to <place>", "send to <place>".
_DELIVERY_CITY_RE = _re_addr.compile(
    r"\b(?:deliver(?:y)?|ship|send)\s+(?:it\s+)?(?:to|in|at)\s+([A-Za-z][A-Za-z\s]{1,25})",
    _re_addr.IGNORECASE,
)


def _extract_delivery_city(text: str) -> str | None:
    """Return the place name from a 'deliver to <city>?' question, or None."""
    match = _DELIVERY_CITY_RE.search(text)
    if not match:
        return None
    city = match.group(1).strip(" ?.,!")
    # Drop a trailing question word that sometimes gets captured, e.g. "Mumbai please".
    city = _re_addr.sub(r"\s+(please|pls|too|also)$", "", city, flags=_re_addr.IGNORECASE).strip()
    return city or None


def _build_order_aside_answer(
    user_text: str,
    conv,
    client,
    pinned_product,
    delivery_time_str: str,
) -> str:
    """
    Build a deterministic one-line answer to a side-question during an order.

    Uses only facts already in memory / DB — never invents prices or policies.
    Returns the answer string (may be empty if no relevant fact found).
    """
    lower = user_text.lower()
    upi_id = getattr(client, "upi_id", None) if client else None
    prod_price = getattr(pinned_product, "price", None) if pinned_product else None
    prod_name = getattr(pinned_product, "name", None) if pinned_product else None

    qty = getattr(conv, "pending_order_quantity", None) or 0
    total = int(qty * prod_price) if (qty and prod_price) else None

    # Delivery charge / time
    if any(kw in lower for kw in ("delivery charge", "delivery fee", "shipping charge",
                                   "delivery cost", "delivery kitna", "delivery charges")):
        return "Delivery is free."

    if any(kw in lower for kw in ("delivery", "deliver", "shipping", "dispatch",
                                   "kitne din", "kab milega", "kab aayega")):
        dt = delivery_time_str or "3–7 business days"
        city = _extract_delivery_city(user_text)
        if city:
            # No per-city delivery restriction data exists in the catalogue —
            # service is pan-India, so answer the yes/no honestly instead of
            # a generic delivery-time line that ignores the city asked about.
            return f"Yes, we deliver to {city}. Delivery: {dt}."
        return f"Delivery: {dt}."

    # Price / total of current order
    if any(kw in lower for kw in ("total", "kitna total", "kitna amount", "total kitna")):
        if total:
            return f"Your current order total is ₹{total:,}."
        if prod_price:
            return f"{prod_name} is ₹{int(prod_price):,}."

    if any(kw in lower for kw in ("price", "kitna hai", "cost", "rate", "kitne ka")):
        if prod_price:
            return f"{prod_name} is ₹{int(prod_price):,}."

    # UPI / payment
    if any(kw in lower for kw in ("upi", "upi id", "gpay", "phonepe", "paytm")):
        if upi_id:
            return f"UPI ID: {upi_id}"

    # Return / refund policy
    if any(kw in lower for kw in ("return", "refund", "exchange", "wapas")):
        return "We accept returns within 7 days of delivery."

    return ""


def _is_simple_ack(text: str) -> bool:
    """True when the message is a chit-chat acknowledgement with no actionable content."""
    lower = text.lower().strip()
    return lower in _ACK_PHRASES or any(lower.startswith(p + " ") for p in _ACK_PHRASES)


def _detect_change_address_intent(text: str) -> tuple[bool, str | None]:
    """
    Detect a change-address intent in the message.

    Returns (True, address_portion) when a specific new address was provided,
    (True, None) when only intent without address was expressed,
    (False, None) when no change-address intent detected.
    """
    from app.services.conversation_flow import clean_address as _clean_addr_fn

    lower = text.lower().strip()
    # Pure intent without an address
    _PURE_CHANGE_INTENT = (
        "change address", "change my address", "address change karna",
        "address badalna", "address change", "i want to change the address",
        "address change karein", "address change karo",
    )
    if any(pi in lower for pi in _PURE_CHANGE_INTENT):
        # Try to strip intent prefix and see if there's an address left
        stripped = _CHANGE_ADDR_RE.sub("", text.strip()).strip()
        if stripped and stripped.lower() != text.lower().strip():
            return (True, _clean_addr_fn(stripped))
        return (True, None)

    # Intent with embedded address — strip everything UP TO AND INCLUDING the
    # matched change-phrase (wherever it occurs, even after a lead-in like
    # "Please"/"Sure,"), not just when the phrase is at position 0. An
    # anchored-only strip leaves "Please change it to <addr>" untouched and
    # the raw command text would otherwise be stored as the address.
    _intent_match = _CHANGE_ADDR_INTENT_RE.search(text)
    if _intent_match:
        remainder = text[_intent_match.end():].strip().lstrip(",:-").strip()
        cleaned = _clean_addr_fn(remainder) if remainder else ""
        return (True, cleaned if cleaned else None)

    return (False, None)


def _log_route(
    conv_id: int,
    route: str,
    reason: str,
    extra: str = "",
    tier: int | None = None,
    match_score: float | None = None,
    cache_hit: bool | None = None,
) -> None:
    """
    Log route=TEMPLATE|LLM for every turn so call reduction can be measured.

    tier/match_score/cache_hit are optional cost-cascade fields (see the
    tiered routing in app/routers/webhook.py): tier 0=deterministic,
    1=catalog match, 2=cheap classify, 3=70B reply.
    """
    parts = [extra] if extra else []
    if tier is not None:
        parts.append(f"tier={tier}")
    if match_score is not None:
        parts.append(f"match_score={match_score:.2f}")
    if cache_hit is not None:
        parts.append(f"cache_hit={cache_hit}")
    extra_part = f" {' '.join(parts)}" if parts else ""
    logger.info("ROUTE conv=%s route=%s reason=%s%s", conv_id, route, reason, extra_part)


def _today_utc() -> str:
    """Return today's UTC date as 'YYYY-MM-DD' string."""
    return datetime.utcnow().strftime("%Y-%m-%d")


# ── Per-phone/day LLM budget caps (Improvement 3) ─────────────────────────
# Soft cap: degrade to template-only for browsing stages (order flow unaffected).
# Hard cap: stop ALL LLM, send boundary message, escalate.
# Both are client-configurable via client.llm_soft_cap / client.llm_hard_cap.
_DEFAULT_LLM_SOFT_CAP = 40   # LLM-calling turns per phone per UTC day
_DEFAULT_LLM_HARD_CAP = 80


async def _check_and_reset_llm_budget(
    db,
    conv,
    client,
) -> tuple[int, int, int]:
    """
    Reset daily counter when date changes; return (calls_today, soft_cap, hard_cap).

    Safe to call repeatedly — only writes to DB when the date rolled over.
    """
    today = _today_utc()
    if (conv.llm_calls_date or "") != today:
        conv.llm_calls_today = 0
        conv.llm_calls_date = today
        try:
            await conversation_service.update_order_field(db, conv.id, "llm_calls_today", 0)
            await conversation_service.update_order_field(db, conv.id, "llm_calls_date", today)
        except Exception as _exc:
            logger.error("LLM budget daily reset failed for conv=%s: %s", conv.id, _exc)
    soft = int(getattr(client, "llm_soft_cap", None) or _DEFAULT_LLM_SOFT_CAP) if client else _DEFAULT_LLM_SOFT_CAP
    hard = int(getattr(client, "llm_hard_cap", None) or _DEFAULT_LLM_HARD_CAP) if client else _DEFAULT_LLM_HARD_CAP
    return (conv.llm_calls_today or 0), soft, hard


# Keywords that indicate AI prematurely jumped to payment/confirmation.
# Checked only when next_slot is not None (slots still incomplete).
_AI_BYPASS_KEYWORDS: frozenset[str] = frozenset({
    "pay via upi", "pay via gpay", "pay via phonepe", "pay via paytm",
    "noted your order", "order confirmed", "order has been confirmed",
    "order is confirmed", "confirmed your order", "your order is placed",
    "order placed", "delivery in", "dispatch", "payment instructions",
    "upi id:", "scan to pay", "please pay ₹", "please pay rs",
    # Payment-jump phrases — AI skips to payment language before all slots are filled
    "total comes out to be", "total is ₹", "total amount is",
    "proceed with payment", "would you like to proceed",
    "please proceed", "aap payment", "payment kar sakte",
})

# ── Per-slot attempt cap constants (Improvement 1) ────────────────────────
_SLOT_ATTEMPT_ESCAPE_HATCH = 3   # append escape hatch at this attempt number
_SLOT_ATTEMPT_ESCALATE = 6       # stop LLM and escalate at this attempt number

# ── Off-topic counter threshold (Improvement 2) ───────────────────────────
_DEFAULT_OFF_TOPIC_THRESHOLD = 4  # consecutive off-topic messages before template-only

# ── Minimum score for auto-pinning/switching a product by name-match (Improvement 4) ──
_NAME_MATCH_AUTO_PIN_MIN_SCORE = 3   # min score for first-time pin (currently always fires when _single_strong)
_NAME_MATCH_CONFIDENCE_FLOOR = 0.4   # min (score / max-possible-score) to offer a name-matched product (Issue D)
_NAME_MATCH_SWITCH_MIN_SCORE = 6     # min score required to SWITCH from already-pinned product silently


def _build_slot_question(
    next_slot: str | None,
    conv,
    variant_info: dict,
    lang: str,
    customer_profile=None,
    accepts_cod: bool = False,
    available_stock: int | None = None,
    product_name: str = "the product",
    declined_saved_address: bool = False,
    attempt_count: int = 0,
) -> str:
    """
    Return the deterministic, language-correct question for the given slot.

    Used by Part 1 to produce the customer-facing message for each slot in the
    order_collection / awaiting_final_confirmation stages — no AI call needed.

    Args:
        next_slot:        Current unfilled slot name (or None if all filled).
        conv:             Conversation ORM instance.
        variant_info:     Dict from catalogue_service.get_product_variant_info.
        lang:             Customer language key.
        customer_profile: Optional CustomerProfile ORM instance (used for saved address).

    Returns:
        Template-rendered question string.
    """
    from app.services.language_templates import get_template as _gt

    vi = variant_info or {}

    if next_slot == "quantity_invalid":
        # available_stock==0 should already be caught upstream by the product/
        # combo OOS gates before quantity is ever asked, but guard here too —
        # "1–0" is a nonsensical range and must never reach the customer.
        # (available_stock is None means untracked stock hitting the sanity
        # ceiling, not zero stock — that case keeps the "limited" wording.)
        if available_stock is not None and available_stock <= 0:
            return _gt(lang, "out_of_stock_block", product=product_name)
        stock_str = str(available_stock) if available_stock is not None else "limited"
        return _gt(lang, "quantity_exceeds_stock", stock=stock_str, product=product_name)

    if next_slot == "color":
        colors = " / ".join(vi.get("available_colors", [])) or "see catalogue"
        _slot_reply = _gt(lang, "ask_color", colors=colors)
        if attempt_count >= _SLOT_ATTEMPT_ESCAPE_HATCH:
            _slot_reply += "\n\n(Reply 'cancel' to stop, or 'help' to reach our team.)"
        return _slot_reply

    if next_slot == "size":
        sizes = " / ".join(vi.get("available_sizes", [])) or "see catalogue"
        _slot_reply = _gt(lang, "ask_size", sizes=sizes)
        if attempt_count >= _SLOT_ATTEMPT_ESCAPE_HATCH:
            _slot_reply += "\n\n(Reply 'cancel' to stop, or 'help' to reach our team.)"
        return _slot_reply

    if next_slot == "material":
        materials = " / ".join(vi.get("available_materials", [])) or "see catalogue"
        _slot_reply = _gt(lang, "ask_material", materials=materials)
        if attempt_count >= _SLOT_ATTEMPT_ESCAPE_HATCH:
            _slot_reply += "\n\n(Reply 'cancel' to stop, or 'help' to reach our team.)"
        return _slot_reply

    # ── Phase 1 cart engine — only reachable when qty > 1 on a variant product ──
    if next_slot == "variant_mode":
        _n = getattr(conv, "pending_order_quantity", 0) or 0
        _cs_parts = [p for p in [getattr(conv, "selected_color", None), getattr(conv, "selected_size", None)] if p]
        _color_size = "/".join(_cs_parts) or "your selection"
        _slot_reply = _gt(lang, "ask_variant_mode", n=_n, color_size=_color_size)
        if attempt_count >= _SLOT_ATTEMPT_ESCAPE_HATCH:
            _slot_reply += "\n\n(Reply 'cancel' to stop, or 'help' to reach our team.)"
        return _slot_reply

    if next_slot == "cart_item_color":
        item_num = len(getattr(conv, "cart_items", None) or []) + 1
        colors = " / ".join(vi.get("available_colors", [])) or "see catalogue"
        return _gt(lang, "ask_cart_item_color", item_num=item_num, colors=colors)

    if next_slot == "cart_item_size":
        item_num = len(getattr(conv, "cart_items", None) or []) + 1
        sizes = " / ".join(vi.get("available_sizes", [])) or "see catalogue"
        return _gt(lang, "ask_cart_item_size", item_num=item_num, sizes=sizes)

    if next_slot == "cart_item_material":
        materials = " / ".join(vi.get("available_materials", [])) or "see catalogue"
        return _gt(lang, "ask_material", materials=materials)

    if next_slot == "cart_item_qty":
        _n = getattr(conv, "pending_order_quantity", 0) or 0
        _remaining = _n - conversation_flow._cart_committed_qty(conv)
        _cart_items_so_far = getattr(conv, "cart_items", None) or []
        _wip = getattr(conv, "cart_wip_item", None) or {}
        if not _cart_items_so_far:
            # Item 1 — color/size/material are the leading slots already
            # answered above, never re-asked here.
            _cs_parts = [p for p in [getattr(conv, "selected_color", None), getattr(conv, "selected_size", None)] if p]
        else:
            _cs_parts = [p for p in [_wip.get("color"), _wip.get("size")] if p]
        _color_size = "/".join(_cs_parts) or "this item"
        return _gt(lang, "ask_cart_item_qty", color_size=_color_size, remaining=_remaining)

    if next_slot == "cart_breakdown":
        _n = getattr(conv, "pending_order_quantity", 0) or 0
        _remaining = _n - conversation_flow._cart_committed_qty(conv)
        return _gt(lang, "ask_cart_breakdown", remaining=_remaining)

    if next_slot == "cart_breakdown_confirm":
        _proposed = getattr(conv, "cart_pending_confirmation", None) or []
        _desc = " + ".join(f"{i['qty']} {i.get('color', '')}".strip() for i in _proposed)
        return _gt(lang, "ask_cart_breakdown_confirm", breakdown=_desc)

    if next_slot == "cart_breakdown_mismatch":
        _info = conv.__dict__.get("_cart_breakdown_mismatch_info") or {}
        return _gt(lang, "cart_breakdown_mismatch", sum=_info.get("sum", "?"), remaining=_info.get("remaining", "?"))

    if next_slot == "cart_breakdown_invalid":
        _info = conv.__dict__.get("_cart_breakdown_invalid_info") or {}
        return _gt(
            lang, "cart_breakdown_invalid_variant",
            invalid=_info.get("invalid", "?"), colors=_info.get("colors", ""), sizes=_info.get("sizes", ""),
        )

    if next_slot == "combo_oos":
        _sel_color = getattr(conv, "selected_color", None) or ""
        _sel_size = getattr(conv, "selected_size", None) or ""
        _sel_material = getattr(conv, "selected_material", None) or ""
        _combo_parts = [p for p in [_sel_color, _sel_size, _sel_material] if p]
        _combo_str = "/".join(_combo_parts)
        if not _combo_str:
            logger.warning("combo_oos fired with empty combo for conv=%s — falling back to ask_color", getattr(conv, "id", "?"))
            return _gt(lang, "ask_color", colors="see catalogue")

        # Fix C: use the stashed cleared-field name to determine what to re-ask,
        # instead of inferring from current slot state (which is already cleared).
        _vi_oos = variant_info or {}
        _cleared = conv.__dict__.get("_combo_oos_cleared_field", "")
        if _cleared == "selected_size":
            _last_attr = "size"
        elif _cleared == "selected_material":
            _last_attr = "material"
        elif _cleared == "selected_color":
            _last_attr = "colour"
        else:
            _last_attr = "material" if _sel_material else ("size" if _sel_size else "colour")

        _avail_opts = conv.__dict__.get("_combo_oos_avail_opts") or {}
        _avail_sizes = _avail_opts.get("sizes") or []
        _avail_colors = _avail_opts.get("colors") or []

        if _last_attr == "size":
            if _avail_sizes and _sel_color:
                # Fix C: say "available only in X" not "sold out"
                _opts_str = " / ".join(_avail_sizes)
                # Fix D: always offer color-switch escape to break the loop
                _other_colors = [c for c in _vi_oos.get("available_colors", []) if c != _sel_color]
                _color_hint = (
                    f" Or pick a different colour: {' / '.join(_other_colors)}."
                    if _other_colors else ""
                )
                return (
                    f"{_sel_color} is available only in {_opts_str}. "
                    f"Which size?{_color_hint}"
                )
            # All sizes for this color are OOS — Fix C+D: tell customer clearly
            _other_colors = [c for c in _vi_oos.get("available_colors", []) if c != _sel_color]
            if _other_colors:
                return (
                    f"{_sel_color or 'This colour'} is fully out of stock. "
                    f"Available colours: {' / '.join(_other_colors)}. Which colour would you like?"
                )

        if _last_attr == "colour" and _avail_colors and _sel_size:
            _opts_str = " / ".join(_avail_colors)
            return (
                f"{_sel_size} is not available in {_sel_color or 'that colour'}. "
                f"Available colours in {_sel_size}: {_opts_str}. Which colour would you like?"
            )

        return _gt(lang, "out_of_stock_combo", combo=_combo_str, last_attr=_last_attr)

    if next_slot == "quantity":
        return _gt(lang, "ask_quantity")

    if next_slot == "customer_name":
        return _gt(lang, "ask_name")

    if next_slot == "delivery_address":
        if not declined_saved_address:
            saved_addr = getattr(customer_profile, "address", None) if customer_profile else None
            if saved_addr:
                _known_name = getattr(conv, "customer_name", None)
                if _known_name:
                    return f"{_known_name}, deliver to: {saved_addr}? (yes/change)"
                return _gt(lang, "confirm_saved_address", address=saved_addr)
        return _gt(lang, "ask_address")

    if next_slot == "mobile_number":
        return _gt(lang, "ask_mobile")

    if next_slot == "payment_method":
        if not accepts_cod:
            # Single implied option — use confirm-style so affirmative fills UPI.
            return _gt(lang, "confirm_payment_upi")
        # Both UPI and COD available — ask for explicit choice.
        return _gt(lang, "ask_payment_upi_cod")

    # None — all slots collected, caller should show order summary instead.
    return ""


# ---------------------------------------------------------------------------
# SLICE 2: early guards
# ---------------------------------------------------------------------------
# Each guard below previously did, inline in webhook.py's receive_message:
#   await conversation_service.save_message(...)   # (sometimes)
#   await outbound.send_text(sender_phone, reply, ...)
#   return {"status": "ok"}
# Converted to: persist whatever DB state the original code persisted, then
# return a PipelineResult instead of sending. webhook.py's adapter is
# responsible for actually calling the gated outbound send with
# the returned text and then returning early. Guards that don't fire return
# None so the caller continues to the next stage of receive_message.
#
# NOTE: signature verification, payload parsing, sender/wamid/message-type/
# text extraction and button-nonce decoding happen BEFORE this point in
# webhook.py and are NOT moved — this function only consumes already-parsed
# values, per the architecture.
# ---------------------------------------------------------------------------


async def run_hard_llm_cap_guard(
    db,
    conv,
    client,
    sender_phone: str,
    user_text: str,
    wamid: str | None,
    llm_calls_today: int,
) -> "PipelineResult | None":
    """
    Hard LLM-budget guard: block all LLM, escalate to human, and reply with a
    boundary message when the per-phone/day hard cap has been hit.

    Mirrors webhook.py's original inline HARD LLM cap block verbatim (DB
    writes, message saves, logging) — only the final send is deferred to the
    caller via the returned PipelineResult.
    """
    _hard_boundary = "We've noted your interest — our team will get back to you."
    logger.warning(
        "HARD LLM cap: phone=%s conv=%s calls_today=%d — blocking all LLM and escalating.",
        sender_phone, conv.id, llm_calls_today,
    )
    try:
        from app.models.conversation import Conversation as _ConvHardCap
        _hc_r = await db.execute(select(_ConvHardCap).where(_ConvHardCap.id == conv.id).limit(1))
        _hc_c = _hc_r.scalar_one_or_none()
        if _hc_c:
            _hc_c.ai_enabled = False
            _hc_c.taken_over_at = datetime.utcnow()
            _hc_c.taken_over_note = f"Hard LLM cap reached: {llm_calls_today} calls"
            await db.commit()
    except Exception as _hce:
        logger.error("Hard-cap takeover failed: %s", _hce)
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _hard_boundary)
    except Exception:
        pass
    return PipelineResult(text=_hard_boundary)


async def run_stale_paid_guard(
    db,
    conv,
    sender_phone: str,
    user_text: str,
    wamid: str | None,
    stored_stage: str,
) -> "PipelineResult | None":
    """
    BUG 2 FIX: reply to a stale "I've Paid" tap with a friendly message and
    no state change, when there's no pending_payment order to mark paid.

    Returns None when the guard does not apply (not a paid-signal, in
    payment stage already, or a payable order does exist).
    """
    _is_paid_signal = (
        user_text.lower().strip() in {
            "paid", "done", "sent", "transferred",
            "ho gaya", "kar diya", "payment done",
        }
        and stored_stage not in ("payment",)
    )
    if not _is_paid_signal:
        return None

    from app.models.order import Order as _PaidGuardOrderModel
    _pg_result = await db.execute(
        select(_PaidGuardOrderModel)
        .where(
            _PaidGuardOrderModel.conversation_id == conv.id,
            _PaidGuardOrderModel.status == "pending_payment",
        )
        .limit(1)
    )
    _pg_order = _pg_result.scalar_one_or_none()
    if _pg_order is not None:
        return None

    _pg_lang = getattr(conv, "last_customer_language", None) or "english"
    if _pg_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
        _pg_reply = "Woh order cancel ho chuka tha. Kya aap naya order dena chahte hain?"
    elif _pg_lang in ("gujarati_roman", "gujarati_script"):
        _pg_reply = "Pehelo order cancel thai gayo hato. Navo order karva maango chho?"
    else:
        _pg_reply = "That order was already cancelled — want to start a new one?"
    logger.info(
        "Stale 'paid' signal from %s — no pending_payment order for conv=%s, "
        "replying friendly, no state change.",
        sender_phone, conv.id,
    )
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _pg_reply)
    except Exception as exc:
        logger.error("Stale-paid save error: %s", exc)
    return PipelineResult(text=_pg_reply)


async def run_button_nonce_guard(
    db,
    conv,
    sender_phone: str,
    user_text: str,
    wamid: str | None,
    stored_stage: str,
    btn_nonce_parsed: "tuple[str, str, str] | None",
) -> "PipelineResult | None":
    """
    BUG 4 FIX: reject a stale/out-of-order nonce-encoded button tap.

    Rotates the nonce immediately on a VALID tap (returns None in that case
    so the caller continues processing); returns a PipelineResult with the
    "expired" message when the nonce does not match.
    """
    if btn_nonce_parsed is None:
        return None
    _p_action, _p_conv_id_str, _p_nonce = btn_nonce_parsed
    _stored_nonce = getattr(conv, "current_button_nonce", None)
    if _stored_nonce and _p_nonce != _stored_nonce:
        _expired_reply = (
            "That option has expired — your last action already went through. "
            "Let's continue from here."
        )
        logger.info(
            "Expired button nonce: conv=%s stored=%r received=%r action=%r — rejected.",
            conv.id, _stored_nonce, _p_nonce, _p_action,
        )
        logger.info(
            "Stale button ignored conv=%s btn=%r stage=%s",
            conv.id, _p_action, stored_stage,
        )
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _expired_reply)
        except Exception:
            pass
        return PipelineResult(text=_expired_reply)
    # Valid nonce — rotate immediately so the same button cannot fire twice.
    if _stored_nonce:
        await _rotate_nonce(db, conv.id, conv)
    return None


async def run_cancel_in_payment_guard(
    db,
    conv,
    client,
    sender_phone: str,
    user_text: str,
    wamid: str | None,
    stored_stage: str,
    record_usage,
) -> "PipelineResult | None":
    """
    Cancel-in-payment guard: handle cancel-intent tapped/typed while
    stage="payment" (pending UPI confirmation) or "awaiting_switch_confirm"
    (mid-payment switch-confirm prompt still open) — cancels the
    pending_payment Order, resets order slots, sets stage back to "greeting".

    Matches on is_cancel_intent(), not just the literal "cancel" button
    payload, so free-text phrasing ("I don't want this", "never mind") is
    caught too — previously only an exact "cancel" fell through to here and
    everything else silently re-showed the payment reminder.

    Also covers "awaiting_switch_confirm" so a cancel-intent reply to the
    switch-confirm prompt cancels the WHOLE order rather than being treated
    as an unclear yes/no (which run_switch_confirm_guard would otherwise
    interpret as "decline switch, keep original order"). This guard runs
    before run_switch_confirm_guard, so it intercepts first.

    record_usage: the webhook module's _record_usage(db, client) callable,
    passed in rather than imported, since it stays defined in webhook.py.
    """
    if not (
        stored_stage in ("payment", "awaiting_switch_confirm")
        and is_cancel_intent(user_text)
    ):
        return None

    from app.models.order import Order as _PayCancelOrderModel
    try:
        _pc_result = await db.execute(
            select(_PayCancelOrderModel)
            .where(
                _PayCancelOrderModel.conversation_id == conv.id,
                _PayCancelOrderModel.status == "pending_payment",
            )
            .order_by(_PayCancelOrderModel.created_at.desc())
            .limit(1)
        )
        _pc_order = _pc_result.scalar_one_or_none()
        if _pc_order:
            _pc_order.status = "cancelled"
            await db.commit()
            logger.info(
                "Order %s → cancelled (conv=%s) [PAYMENT-STAGE CANCEL]",
                _pc_order.order_number, conv.id,
            )
    except Exception as exc:
        logger.error("Payment-stage cancel: order cancellation failed for conv=%s: %s", conv.id, exc)

    _pc_cancel_fields = [
        ("pending_order_quantity", None), ("selected_color", None),
        ("selected_size", None), ("selected_material", None),
        ("customer_name", None), ("delivery_address", None),
        ("payment_method", None), ("summary_shown", False),
        ("pending_product_sku", None), ("interrupted_sku", None),
        ("last_shown_sku", None), ("pending_choice_skus", None),
        ("cart_items", None), ("cart_variant_mode", None),
        ("cart_collection_mode", None), ("cart_wip_item", None),
        ("cart_pending_confirmation", None),
    ]
    for _pcf, _pcv in _pc_cancel_fields:
        try:
            await conversation_service.update_order_field(db, conv.id, _pcf, _pcv)
            setattr(conv, _pcf, _pcv)
        except Exception as exc:
            logger.error("Payment-stage cancel slot reset (%s): %s", _pcf, exc)
    try:
        await conversation_service.update_stage(db, conv.id, "greeting")
    except Exception as exc:
        logger.error("Payment-stage cancel stage reset: %s", exc)

    _pc_lang = getattr(conv, "last_customer_language", None) or "english"
    if _pc_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
        _pc_reply = "Order cancel kar diya gaya. ✅ Kya main kuch aur help kar sakta hoon?"
    elif _pc_lang in ("gujarati_roman", "gujarati_script"):
        _pc_reply = "Order cancel thai gayu. ✅ Koi biju kaam hoy to kaho!"
    else:
        _pc_reply = "Order cancelled. ✅ Anything else I can help you with?"
    logger.info(
        "conv=%s PAYMENT-STAGE CANCEL (from stage=%s) — order cancelled, slots reset, stage=greeting",
        conv.id, stored_stage,
    )
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _pc_reply)
    except Exception as exc:
        logger.error("Payment-stage cancel save error: %s", exc)
    try:
        await record_usage(db, client, conv)
    except Exception:
        pass
    return PipelineResult(text=_pc_reply)


async def run_blocklist_guard(db, client, sender_phone: str) -> bool:
    """
    Upsert the customer profile (best-effort) and report whether they are
    blocklisted.

    Returns True when the customer is blocked (caller should drop the
    message with no reply at all — matches original "message dropped, no
    send" behaviour exactly, hence bool rather than PipelineResult).
    """
    if not client:
        return False
    try:
        customer = await customer_service.upsert_customer(
            db,
            client_id=client.id,
            phone=sender_phone,
        )
        await db.commit()
        if customer.is_blocked:
            logger.info("Blocked customer %s — message dropped.", sender_phone)
            return True
    except Exception as exc:
        logger.warning("Customer upsert failed: %s", exc)
    return False


async def run_human_takeover_guard(
    db, conv, sender_phone: str, user_text: str, wamid: str | None
) -> "PipelineResult | None":
    """
    Human takeover guard: when conv.ai_enabled is False, silently save the
    inbound message and produce a PipelineResult that sends nothing at all
    (skip_send=True) — mirrors the original "save message, no reply" path.
    """
    if conv.ai_enabled is not False:
        return None
    await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
    logger.info(
        "AI paused for %s — human takeover active, message saved silently.",
        sender_phone,
    )
    return PipelineResult(text=None, skip_send=True)


async def run_duplicate_confirm_tap_guard(
    db, conv, message_type: str, sender_phone: str, user_text: str, wamid: str | None
) -> "PipelineResult | None":
    """
    Duplicate "Confirm Order" tap guard: an already-tappable button pressed
    again after the conversation reached "completed" gets a friendly
    already-confirmed reply with no AI call.
    """
    _CONFIRM_TAP_KW = frozenset({"confirm order", "✅ confirm order", "confirm", "pakka"})
    if not (
        message_type == "interactive"
        and (conv.current_stage or "greeting") == "completed"
        and user_text.lower().strip() in _CONFIRM_TAP_KW
    ):
        return None
    _already_confirmed_reply = (
        "Your order is already confirmed ✅ We'll update you once it's dispatched. "
        "Thank you! 🙏"
    )
    logger.info(
        "Duplicate 'Confirm Order' tap from %s on already-completed conv=%s — "
        "sending friendly reminder, no AI call.",
        sender_phone, conv.id,
    )
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _already_confirmed_reply)
    except Exception as exc:
        logger.error("DB save error for duplicate confirm tap: %s", exc)
    return PipelineResult(text=_already_confirmed_reply)


async def run_duplicate_payment_word_guard(
    db, conv, message_type: str, sender_phone: str, user_text: str, wamid: str | None
) -> "PipelineResult | None":
    """
    FIX E: payment-confirmation words sent AFTER the order is already
    completed (e.g. a second "paid"/"done" tap) get a deterministic
    already-confirmed reply instead of a fresh AI call or duplicate Order row.
    """
    _PAYMENT_RECONFIRM_KW = frozenset({
        "paid", "done", "sent", "transferred", "ho gaya", "kar diya",
        "completed", "payment done", "bhej diya", "kiya", "payment ho gaya",
        "payment kar diya", "gpay", "phonepay", "paytm", "phonepe",
    })
    if not (
        message_type == "text"
        and (conv.current_stage or "greeting") == "completed"
        and user_text.lower().strip() in _PAYMENT_RECONFIRM_KW
    ):
        return None
    _prev_lang = getattr(conv, "last_customer_language", "english") or "english"
    if _prev_lang in ("hindi_roman", "hinglish", "hindi_devanagari"):
        _payment_dup_reply = (
            "Aapka order confirm ho chuka hai ✅ Delivery 3-5 business days mein hogi. "
            "Koi aur help chahiye? 🙏"
        )
    elif _prev_lang in ("gujarati_roman", "gujarati_script"):
        _payment_dup_reply = (
            "Tamaro order confirm thai gayo chhe ✅ Delivery 3-5 business days maa thase. "
            "Koi madad joiye? 🙏"
        )
    else:
        _payment_dup_reply = (
            "Your order is already confirmed ✅ Delivery in 3-5 business days. "
            "Anything else I can help with? 🙏"
        )
    logger.info(
        "Duplicate payment word '%s' from %s on already-completed conv=%s — "
        "returning deterministic reply, no AI call.",
        user_text, sender_phone, conv.id,
    )
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _payment_dup_reply)
    except Exception as exc:
        logger.error("DB save error for duplicate payment reply: %s", exc)
    return PipelineResult(text=_payment_dup_reply)


# ---------------------------------------------------------------------------
# Mid-payment product-switch confirmation
# ---------------------------------------------------------------------------
# When the customer expresses new-purchase/switch intent ("I want to buy X")
# while stage="payment" (see the is_purchase_intent branch inside the
# payment-stage dispatch below), we do NOT silently overwrite the pending
# order. Instead we stash the candidate SKU in interrupted_sku — the same
# field the browsing-stage interrupt-switch flow already uses — set
# current_stage to the 'awaiting_switch_confirm' micro-stage, and ask the
# customer to confirm. This guard resolves that confirmation on the
# customer's NEXT turn, entirely before detect_stage/SKU-pinning run, so it
# never needs to teach the wider state machine about the new stage value.
# ---------------------------------------------------------------------------

async def run_switch_confirm_guard(
    db, conv, client, sender_phone: str, user_text: str, wamid: "str | None", record_usage,
) -> "PipelineResult | None":
    """
    Resolve a pending mid-payment switch-confirmation prompt.

    Returns None (caller continues normal dispatch) whenever current_stage
    isn't 'awaiting_switch_confirm' — i.e. on every ordinary turn.
    """
    if (getattr(conv, "current_stage", None) or "") != "awaiting_switch_confirm":
        return None

    _candidate_sku = getattr(conv, "interrupted_sku", None)
    _lang = getattr(conv, "last_customer_language", None) or "english"
    _confirm_yes = user_text.strip().lower() in {
        "yes", "haan", "ha", "han", "ok", "okay", "sure", "y", "yep", "yeah",
        "bilkul", "हाँ", "ہاں",
    }

    if _confirm_yes and _candidate_sku:
        try:
            await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _candidate_sku)
            conv.pending_product_sku = _candidate_sku
            await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
            conv.interrupted_sku = None
            await conversation_service.update_stage(db, conv.id, "payment")
            conv.current_stage = "payment"
        except Exception as exc:
            logger.error("Switch-confirm yes error: %s", exc)
        _render_action_sc = "show_payment"
        logger.info("Switch confirmed: conv=%s → pending_product_sku=%s", conv.id, _candidate_sku)
    else:
        try:
            await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
            conv.interrupted_sku = None
            await conversation_service.update_stage(db, conv.id, "payment")
            conv.current_stage = "payment"
        except Exception as exc:
            logger.error("Switch-confirm no/other error: %s", exc)
        _render_action_sc = "reask_payment"
        logger.info(
            "Switch declined/unclear: conv=%s — kept pending_product_sku=%r",
            conv.id, getattr(conv, "pending_product_sku", None),
        )

    try:
        reply = await _render_order_reply(
            action=_render_action_sc, conv=conv, db=db, client=client,
            next_slot=None, variant_info={}, customer_profile=None,
            available_stock=None, declined_saved_address=False, lang=_lang,
        )
    except RenderError as exc:
        logger.error("Switch-confirm render error: %s", exc)
        return PipelineResult(text=None, skip_send=True, status="render_error")

    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply)
    except Exception as exc:
        logger.error("Switch-confirm save error: %s", exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:
        logger.error("Usage tracking error (switch confirm): %s", exc)
    return PipelineResult(text=reply)


# ---------------------------------------------------------------------------
# Cross-product aside-question switch confirmation
# ---------------------------------------------------------------------------
# When a customer asks about a DIFFERENT, named product while a slot is
# pending for the pinned order (FIX2/FIX4 in run_slot_state_machine, e.g.
# "LH10042 is available?" while a chair is mid quantity-slot), we answer the
# question and then ask "Would you like to order <named product>? (Yes/No)"
# instead of confusingly re-asking the ORIGINAL pinned slot. The candidate
# SKU is stashed in interrupted_sku (the same field the payment-stage
# run_switch_confirm_guard uses) under a dedicated micro-stage,
# 'awaiting_order_switch_confirm' — distinct from payment's
# 'awaiting_switch_confirm' since resolution must return to order_collection
# slot-filling, not payment. This guard resolves that confirmation on the
# customer's NEXT turn, entirely before SKU/name-pinning runs, so a bare
# "yes"/"no" is never mistaken for a fresh SKU search or swallowed by the
# browsing-stage interrupted_sku handler (run_sku_and_name_pinning).
# ---------------------------------------------------------------------------

async def run_order_switch_confirm_guard(
    db, conv, client, sender_phone: str, user_text: str, wamid: "str | None", record_usage,
) -> "PipelineResult | None":
    """
    Resolve a pending cross-product order-switch confirmation prompt.

    Returns None (caller continues normal dispatch) whenever current_stage
    isn't 'awaiting_order_switch_confirm' — i.e. on every ordinary turn.
    """
    if (getattr(conv, "current_stage", None) or "") != "awaiting_order_switch_confirm":
        return None

    _candidate_sku = getattr(conv, "interrupted_sku", None)
    _lang = getattr(conv, "last_customer_language", None) or "english"
    _confirm_yes = user_text.strip().lower() in {
        "yes", "haan", "ha", "han", "ok", "okay", "sure", "y", "yep", "yeah",
        "bilkul", "हाँ", "ہاں",
    }

    _new_product = None
    if _confirm_yes and _candidate_sku and client:
        _new_product = await catalogue_service.find_product_by_sku(db, client.id, _candidate_sku)

    if _new_product is not None:
        # Reuse the NEW_PRODUCT re-pin primitive (see run_slot_state_machine's
        # NEW_PRODUCT branch): archive the old SKU, reset variant/qty slots,
        # pin the new SKU, and start slot-filling for it.
        _old_sku = getattr(conv, "pending_product_sku", None)
        if _old_sku and _old_sku != _candidate_sku:
            _browsed_raw = getattr(conv, "browsed_skus", None) or "[]"
            try:
                _browsed = _json.loads(_browsed_raw)
            except Exception:
                _browsed = []
            if _old_sku not in _browsed:
                _browsed.append(_old_sku)
            try:
                _nb = _json.dumps(_browsed)
                await conversation_service.update_order_field(db, conv.id, "browsed_skus", _nb)
                conv.browsed_skus = _nb
            except Exception as exc:
                logger.error("browsed_skus update (cross-product switch): %s", exc)
        _reset_fields = [
            ("pending_order_quantity", None), ("selected_color", None),
            ("selected_size", None), ("selected_material", None),
            ("summary_shown", False),
        ]
        for _rf, _rv in _reset_fields:
            try:
                await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                setattr(conv, _rf, _rv)
            except Exception as exc:
                logger.error("cross-product switch slot reset (%s): %s", _rf, exc)
        try:
            await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _candidate_sku)
            conv.pending_product_sku = _candidate_sku
            await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
            conv.interrupted_sku = None
            await conversation_service.update_stage(db, conv.id, "order_collection")
            conv.current_stage = "order_collection"
        except Exception as exc:
            logger.error("Cross-product switch-confirm yes error: %s", exc)
        try:
            _variant_info = await catalogue_service.get_product_variant_info(db, _new_product)
        except Exception as exc:
            logger.error("Cross-product switch variant_info fetch error: %s", exc)
            _variant_info = {}
        _next_slot = conversation_flow.get_next_required_slot(conv, _variant_info)
        _is_first_slot = True
        logger.info(
            "Cross-product switch confirmed: conv=%s → pending_product_sku=%s (was %s)",
            conv.id, _candidate_sku, _old_sku,
        )
    else:
        # "No", an unclear reply, or the candidate no longer resolves — keep
        # the original pinned product/slots untouched and simply re-ask the
        # slot that was pending before the aside-question interrupted it.
        try:
            await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
            conv.interrupted_sku = None
            await conversation_service.update_stage(db, conv.id, "order_collection")
            conv.current_stage = "order_collection"
        except Exception as exc:
            logger.error("Cross-product switch-confirm no/other error: %s", exc)
        _pinned_sku_reask = getattr(conv, "pending_product_sku", None)
        _pinned_product_reask = (
            await catalogue_service.find_product_by_sku(db, client.id, _pinned_sku_reask)
            if (client and _pinned_sku_reask) else None
        )
        try:
            _variant_info = (
                await catalogue_service.get_product_variant_info(db, _pinned_product_reask)
                if _pinned_product_reask else {}
            )
        except Exception as exc:
            logger.error("Cross-product switch decline variant_info fetch error: %s", exc)
            _variant_info = {}
        _next_slot = conversation_flow.get_next_required_slot(conv, _variant_info)
        _is_first_slot = False
        logger.info(
            "Cross-product switch declined/unclear: conv=%s — kept pending_product_sku=%r",
            conv.id, _pinned_sku_reask,
        )

    try:
        reply = await _render_order_reply(
            action="ask_slot", conv=conv, db=db, client=client,
            next_slot=_next_slot, variant_info=_variant_info, customer_profile=None,
            available_stock=None, declined_saved_address=False, lang=_lang,
            is_first_slot=_is_first_slot,
        )
    except RenderError as exc:
        logger.error("Cross-product switch-confirm render error: %s", exc)
        return PipelineResult(text=None, skip_send=True, status="render_error")

    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply)
    except Exception as exc:
        logger.error("Cross-product switch-confirm save error: %s", exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:
        logger.error("Usage tracking error (cross-product switch confirm): %s", exc)
    return PipelineResult(text=reply)


# ---------------------------------------------------------------------------
# Flow-state / context expiry (migration 0052)
# ---------------------------------------------------------------------------
# flow_state (conv.current_stage + the order slots it drives) resets after
# FLOW_STATE_TTL of silence, so a customer who vanishes mid-flow and returns
# later is never dropped back into a stale step — e.g. re-shown/resumed an
# old product pick or asked to keep paying for an order they've forgotten.
#
# last_context (the last product referenced) is a SEPARATE, longer-lived
# snapshot (CONTEXT_TTL) kept alive specifically so pronoun references
# ("that", "it", "still available?") keep resolving even once the active
# flow itself has reset. conversation_service.set_last_context is the single
# writer for those two columns — do not add a second one.
# ---------------------------------------------------------------------------

DEFAULT_FLOW_STATE_TTL = timedelta(hours=24)
DEFAULT_CONTEXT_TTL = timedelta(days=5)

# Fields cleared alongside current_stage on a flow-state reset — the same
# field list already used by the existing cancel/post-order reset blocks
# elsewhere in this module (see e.g. _pc_cancel_fields above). Kept as its
# own constant so there is one place to update if that list changes.
_FLOW_STATE_RESET_FIELDS = (
    "pending_product_sku", "interrupted_sku", "pending_choice_skus",
    "pending_order_quantity", "selected_color", "selected_size",
    "selected_material", "customer_name", "delivery_address",
    "payment_method", "summary_shown",
)

_GREETING_ONLY_PHRASES = frozenset({
    "hi", "hello", "hey", "hii", "hiii", "helo", "hlo",
    "namaste", "namaskar", "namaskte", "salam", "assalam", "kem cho", "hola",
})

# Matched via the compiled regex below (word-boundary, case-insensitive) so
# bare "it"/"that" don't false-positive inside unrelated words.
_REFERENCE_PRONOUN_PHRASES = (
    "still available", "last one", "same one", "this one", "that one",
    "the same", "this", "that", "it",
)
_REFERENCE_PRONOUN_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _REFERENCE_PRONOUN_PHRASES) + r")\b",
    re.IGNORECASE,
)


def is_greeting_only(text: str) -> bool:
    """
    True when *text* is nothing but a bare greeting ("hi", "Hello!", "  hii ")
    — case-insensitive, punctuation-stripped, matched as the ENTIRE message
    (not a substring), so "hi, is that available?" is NOT a bare greeting.
    """
    if not text:
        return False
    stripped = re.sub(r"[^\w\s]", "", text.lower())
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped in _GREETING_ONLY_PHRASES


def has_reference_pronoun(text: str) -> bool:
    """True when *text* refers back to a previously discussed product ("that", "it", "still available?")."""
    return bool(text) and bool(_REFERENCE_PRONOUN_RE.search(text))


def _flow_state_ttl(client) -> timedelta:
    """Per-tenant FLOW_STATE_TTL override (client.flow_state_ttl_hours if set), else DEFAULT_FLOW_STATE_TTL."""
    hours = getattr(client, "flow_state_ttl_hours", None) if client else None
    return timedelta(hours=hours) if hours else DEFAULT_FLOW_STATE_TTL


def _context_ttl(client) -> timedelta:
    """Per-tenant CONTEXT_TTL override (client.context_ttl_days if set), else DEFAULT_CONTEXT_TTL."""
    days = getattr(client, "context_ttl_days", None) if client else None
    return timedelta(days=days) if days else DEFAULT_CONTEXT_TTL


async def _reset_flow_state(db, conv) -> None:
    """
    Reset current_stage to 'greeting' and clear the order slots it drives.

    Deliberately leaves last_context/last_context_at untouched — that
    snapshot has its own, longer TTL (see module docstring above).
    """
    for _field_name in _FLOW_STATE_RESET_FIELDS:
        _default = False if _field_name == "summary_shown" else None
        try:
            await conversation_service.update_order_field(db, conv.id, _field_name, _default)
            setattr(conv, _field_name, _default)
        except Exception as exc:
            logger.error("Flow-state reset error (%s): %s", _field_name, exc)
    try:
        await conversation_service.update_stage(db, conv.id, "greeting")
        conv.current_stage = "greeting"
    except Exception as exc:
        logger.error("Flow-state reset stage-update error: %s", exc)


async def _send_fresh_greeting(
    db, conv, client, sender_phone: str, user_text: str, wamid: "str | None", record_usage,
) -> PipelineResult:
    """
    Deterministic fresh welcome + catalogue link, regenerated on every call
    (never cached) — used when a bare greeting arrives while a stale/active
    flow was in progress. Resets the flow rather than resuming whatever step
    the customer was previously on.
    """
    settings = get_settings()
    slug = getattr(client, "catalogue_slug", None) if client else None
    catalogue_url = f"{settings.catalogue_base_url}/{slug}" if slug else settings.catalogue_base_url
    business = (getattr(client, "business_name", None) or "our store") if client else "our store"
    lang = (getattr(conv, "last_customer_language", None) or "english").lower()
    reply = get_template(lang, "greeting_new", business=business, catalogue_url=catalogue_url)

    _log_route(conv.id, "TEMPLATE", "flow_state_expiry_greeting_reset")
    logger.info(
        "Flow-state greeting reset: conv=%s sender=%s — fresh welcome, no flow resume.",
        conv.id, sender_phone,
    )
    await _reset_flow_state(db, conv)
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply)
    except Exception as exc:
        logger.error("Fresh-greeting save error: %s", exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:
        logger.error("Usage tracking error (fresh greeting): %s", exc)
    return PipelineResult(text=reply)


async def _resolve_last_context_reference(
    db, conv, client, last_context: dict, user_text: str,
    sender_phone: str, wamid: "str | None", record_usage,
) -> "PipelineResult | None":
    """
    Answer a pronoun reference ("is that available?", "still in stock?")
    directly from last_context's snapshot plus a fresh stock/price lookup —
    no stage change, no flow restart.

    Returns None (caller falls through to normal flow dispatch) when the
    product can no longer be found, e.g. it was deleted since last_context
    was captured.
    """
    sku = last_context.get("sku")
    product = await catalogue_service.find_product_by_sku(db, client.id, sku) if (client and sku) else None
    if product is None:
        return None

    lang = (getattr(conv, "last_customer_language", None) or "english").lower()
    stock = getattr(product, "stock", None) or 0
    if stock <= 0:
        reply = get_template(lang, "out_of_stock", product=product.name)
    else:
        reply = get_template(
            lang, "product_found",
            name=product.name, price=int(round(product.price)), stock=stock,
        )

    _log_route(conv.id, "TEMPLATE", "last_context_pronoun_reference", extra=f"sku={sku}")
    logger.info(
        "Pronoun reference resolved from last_context: conv=%s sender=%s sku=%s — direct answer, no flow restart.",
        conv.id, sender_phone, sku,
    )
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply)
    except Exception as exc:
        logger.error("Last-context reference save error: %s", exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:
        logger.error("Usage tracking error (last-context reference): %s", exc)
    return PipelineResult(text=reply)


# ---------------------------------------------------------------------------
# SLICE 3 — SKU / product-name pinning + match
# ---------------------------------------------------------------------------

def _is_valid_image_url(url: str | None) -> bool:
    """Return True only for non-empty http/https URLs."""
    return bool(url) and isinstance(url, str) and url.strip().startswith(("http://", "https://"))


@dataclass
class SkuPinOutcome:
    """
    Result of run_sku_and_name_pinning().

    When early_result is set, the caller must (unless early_result.skip_send
    or early_result.text is falsy) send early_result.text, logging the send
    failure via `logger.error(early_send_error_label, exc)` when a label is
    set or swallowing it silently when not (mirrors the three distinct
    inline send/except styles in the original code) — then return
    {"status": "ok"} without any further processing this turn.

    Otherwise the caller continues, splicing pinned_product / variant_info /
    catalogue_context / canonical_browse_products / pick_just_resolved /
    p03_repinned back into its own locals in place of what it had before
    the call.
    """

    early_result: "PipelineResult | None" = None
    early_send_error_label: str | None = None
    pinned_product: object = None
    variant_info: dict = field(default_factory=lambda: {
        "has_variants": False, "needs_color": False, "needs_size": False,
        "needs_material": False, "available_colors": [], "available_sizes": [],
        "available_materials": [],
    })
    catalogue_context: str = ""
    canonical_browse_products: list = field(default_factory=list)
    pick_just_resolved: bool = False
    p03_repinned: bool = False
    multi_match_this_turn: bool = False


async def run_sku_and_name_pinning(
    db,
    conv,
    client,
    message,
    user_text: str,
    sender_phone: str,
    wamid: str | None,
    stored_stage: str,
    record_usage,
    catalogue_context: str,
    canonical_browse_products: list,
    pending_product_images: list,
    find_sku_matched_products,
    is_product_out_of_stock,
) -> SkuPinOutcome:
    """
    stored_stage: the conversation's stage snapshotted BEFORE this turn's
    mutations (webhook.py's `_stored_stage`) — the interrupted-SKU and
    name-match sub-blocks below gate on this pre-update value, not on
    `conv.current_stage` as mutated by earlier sub-blocks in this same call,
    exactly as in the original inline code.

    Mirrors webhook.py's original inline SKU/product-name pinning block
    verbatim (DB writes, message saves, logging, the three early-return
    paths) — moved here unchanged as part of the strangler-fig extraction.

    `pending_product_images` is mutated in place (entries appended) and
    `conv` is mutated in place via direct attribute sets, exactly as in the
    original inline code. All other per-turn state this block can reassign
    is returned via SkuPinOutcome for the caller to splice back into its
    locals.

    record_usage / find_sku_matched_products / is_product_out_of_stock: the
    webhook module's `_record_usage` / `_find_sku_matched_products` /
    `_is_product_out_of_stock` callables, passed in rather than imported
    since they still live in webhook.py.
    """
    out = SkuPinOutcome(
        catalogue_context=catalogue_context,
        canonical_browse_products=canonical_browse_products,
    )

    # ── Learning 4: when a customer quotes a SKU in a text message, send the
    # product photo first (if one exists) — the AI's text reply follows
    # separately. We deliberately do NOT do this for every message (Learning 1:
    # never send unsolicited images) — only on an explicit SKU match.
    if message.type == "text" and user_text:
        sku_products = await find_sku_matched_products(db, client, user_text)
        # Pin the matched SKU so subsequent short replies (e.g. "yes", "ok") still
        # refer to the same product rather than triggering a fresh keyword search.
        # Guard: once in order_collection or payment, NEVER change the pinned SKU
        # — a compound-paste or casual product question mid-order must not derail the active order.
        _active_order_stages = {"payment", "order_collection", "awaiting_final_confirmation"}
        _can_switch_sku = (
            not getattr(conv, "pending_product_sku", None)
            or (conv.current_stage or "greeting") not in _active_order_stages
        )
        if sku_products and _can_switch_sku:
            if len(sku_products) == 1:
                # Single SKU match → pin it and send product image
                first_sku = getattr(sku_products[0], "sku", None)
                if first_sku:
                    # ── Part 3: OOS check before pinning ────────────────────
                    _oos_product_for_pin = sku_products[0]
                    if await is_product_out_of_stock(db, _oos_product_for_pin):
                        _oos_pin_name = getattr(_oos_product_for_pin, "name", None) or first_sku or "this product"
                        if not _oos_pin_name or not _oos_pin_name.strip():
                            logger.error(
                                "OOS pin: empty product name for SKU=%s conv=%s — suppressing OOS message",
                                first_sku, conv.id,
                            )
                            out.early_result = PipelineResult(text=None, skip_send=True)
                            return out
                        _oos_pin_lang = getattr(conv, "last_customer_language", None) or "english"
                        from app.services.language_templates import get_template as _get_oospin_tpl
                        _oos_pin_reply = _get_oospin_tpl(_oos_pin_lang, "out_of_stock_block", product=_oos_pin_name)
                        logger.info("OOS block at pin: conv=%s SKU=%s", conv.id, first_sku)
                        try:
                            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                            await conversation_service.save_message(db, conv.id, "assistant", _oos_pin_reply)
                        except Exception as exc:
                            logger.error("OOS pin save error: %s", exc)
                        try:
                            await record_usage(db, client, conv)
                        except Exception:
                            pass
                        out.early_result = PipelineResult(text=_oos_pin_reply)
                        out.early_send_error_label = "OOS pin send error: %s"
                        return out
                    # ─────────────────────────────────────────────────────────
                    _old_sku = getattr(conv, "pending_product_sku", None)
                    if first_sku != _old_sku:
                        # Track browsed SKU before switching
                        if _old_sku:
                            _browsed_raw = getattr(conv, "browsed_skus", None) or "[]"
                            try:
                                _browsed = _json.loads(_browsed_raw)
                            except Exception:
                                _browsed = []
                            if _old_sku not in _browsed:
                                _browsed.append(_old_sku)
                            try:
                                _new_browsed = _json.dumps(_browsed)
                                await conversation_service.update_order_field(db, conv.id, "browsed_skus", _new_browsed)
                                conv.browsed_skus = _new_browsed
                            except Exception as exc:
                                logger.error("browsed_skus update error: %s", exc)
                        _order_reset_fields = [
                            ("pending_order_quantity", None),
                            ("selected_color", None),
                            ("selected_size", None),
                            ("selected_material", None),
                            ("customer_name", None),
                            ("delivery_address", None),
                            ("payment_method", None),
                            ("summary_shown", False),
                        ]
                        for _rf, _rv in _order_reset_fields:
                            try:
                                await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                                setattr(conv, _rf, _rv)
                            except Exception as exc:
                                logger.error("New-SKU slot reset (%s): %s", _rf, exc)
                        try:
                            await conversation_service.update_stage(db, conv.id, "product_inquiry")
                            conv.current_stage = "product_inquiry"
                        except Exception as exc:
                            logger.error("New-SKU stage reset: %s", exc)
                        logger.info(
                            "New SKU %s detected (was %s, stage=%s) — reset all order slots.",
                            first_sku, _old_sku, conv.current_stage,
                        )
                    try:
                        await conversation_service.update_order_field(
                            db, conv.id, "pending_product_sku", first_sku
                        )
                        conv.pending_product_sku = first_sku
                    except Exception as exc:
                        logger.error("SKU pin error: %s", exc)
                # Rebuild catalogue_context/canonical_browse_products for the
                # SKU just pinned this turn — without this, both stayed at
                # whatever _get_catalogue_context() resolved from the OLD
                # pending_product_sku (read before this pin ran), so the LLM
                # context and guard_product_reply's canonical set would lag
                # one turn behind an explicit SKU switch (e.g. guard would
                # flag the newly-pinned product's own SKU/price as "phantom").
                out.catalogue_context = catalogue_service.format_catalogue_context(
                    sku_products, for_display=True
                )
                out.canonical_browse_products = sku_products
                for p in sku_products:
                    if _is_valid_image_url(getattr(p, "image_url", None)):
                        pending_product_images.append(
                            (p.image_url, f"{p.name} — ₹{p.price}")
                        )
            else:
                # Multiple SKU matches in browsing — show all images, don't pin to first.
                # Pin the LAST/most recent as pending hint but mark stage browsing;
                # AI will ask "which one?" based on multi-product catalogue context.
                _last_sku = getattr(sku_products[-1], "sku", None)
                if _last_sku and _last_sku != getattr(conv, "pending_product_sku", None):
                    _multi_old_sku = getattr(conv, "pending_product_sku", None)
                    _multi_reset_fields = [
                        ("pending_order_quantity", None), ("selected_color", None),
                        ("selected_size", None), ("selected_material", None),
                        ("customer_name", None), ("delivery_address", None),
                        ("payment_method", None), ("summary_shown", False),
                    ]
                    for _rf, _rv in _multi_reset_fields:
                        try:
                            await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                            setattr(conv, _rf, _rv)
                        except Exception as exc:
                            logger.error("Multi-SKU slot reset (%s): %s", _rf, exc)
                    try:
                        await conversation_service.update_order_field(
                            db, conv.id, "pending_product_sku", _last_sku
                        )
                        conv.pending_product_sku = _last_sku
                    except Exception as exc:
                        logger.error("Multi-SKU last-pin error: %s", exc)
                    try:
                        await conversation_service.update_stage(db, conv.id, "product_inquiry")
                        conv.current_stage = "product_inquiry"
                    except Exception as exc:
                        logger.error("Multi-SKU stage reset: %s", exc)
                    logger.info(
                        "Multi-SKU new pin: conv=%s SKU=%s (was %s) — reset all order slots.",
                        conv.id, _last_sku, _multi_old_sku,
                    )
                # Rebuild catalogue_context to include ALL matched products
                out.catalogue_context = catalogue_service.format_catalogue_context(
                    sku_products, for_display=True
                )
                out.canonical_browse_products = sku_products
                # Queue images for all matched products (sent after text reply)
                for p in sku_products:
                    if _is_valid_image_url(getattr(p, "image_url", None)):
                        pending_product_images.append(
                            (p.image_url, f"{p.name} — ₹{p.price}")
                        )
                logger.info(
                    "Multi-SKU browsing: conv=%s matched %d SKUs — showing all, asking which one.",
                    conv.id, len(sku_products),
                )

    # ── Pending multi-choice: resolve a numbered/name pick, or reject a bare
    # affirmative ("Yes") against an open "which one?" list ─────────────────
    # When two-plus name-matched products were just shown, last_shown_sku was
    # cleared (nothing was confirmed) and pending_choice_skus holds the shown
    # SKUs. A bare "yes" here is NOT a valid answer to "which one?" and must
    # never fall through to the P0-3 repin below, which would otherwise pin a
    # stale last_shown_sku from a completely different, earlier product.
    _pending_choice_skus_list: list = []
    try:
        _pcs_raw = getattr(conv, "pending_choice_skus", None)
        if _pcs_raw:
            _pending_choice_skus_list = _json.loads(_pcs_raw)
    except Exception:
        _pending_choice_skus_list = []

    _PC_ORDINAL_WORDS = ("first", "second", "third", "fourth")

    # FIX 1: interactive button/list taps must resolve here too — a button id
    # equal to a SKU in pending_choice_skus is handled by branch 3 below (SKU
    # substring match) the same as typed "KU76326". Previously this whole block
    # was gated to message.type == "text" only, so a button tap fell straight
    # through to open-browsing/LLM routing (KB search + 70B call) instead of
    # resolving deterministically — the cost regression in conv 52.
    if message.type in ("text", "interactive") and _pending_choice_skus_list:
        _picked_sku = None
        _stripped_pc = user_text.strip()
        _stripped_pc_lower = _stripped_pc.lower()

        # Pre-fetch the candidate products once — reused for ordinal/index,
        # SKU-substring, fuzzy-name resolution, and the re-ask message below.
        _pc_candidates = []
        if client:
            for _cand_sku in _pending_choice_skus_list:
                try:
                    _cand_prod = await catalogue_service.find_product_by_sku(db, client.id, _cand_sku)
                except Exception:
                    _cand_prod = None
                _pc_candidates.append((_cand_sku, _cand_prod))

        # 1. Numeric index: "1", "2" ...
        if _stripped_pc.isdigit():
            _pc_idx = int(_stripped_pc) - 1
            if 0 <= _pc_idx < len(_pending_choice_skus_list):
                _picked_sku = _pending_choice_skus_list[_pc_idx]

        # 2. Ordinal word: "first", "the second one" ...
        if not _picked_sku:
            for _ord_idx, _ord_word in enumerate(_PC_ORDINAL_WORDS):
                if _ord_idx < len(_pending_choice_skus_list) and re.search(
                    r"\b" + _ord_word + r"\b", _stripped_pc_lower
                ):
                    _picked_sku = _pending_choice_skus_list[_ord_idx]
                    break

        # 3. SKU substring match: "KU23444", "the KU23444 one" ...
        if not _picked_sku:
            for _cand_sku, _ in _pc_candidates:
                if _cand_sku and _cand_sku.lower() in _stripped_pc_lower:
                    _picked_sku = _cand_sku
                    break

        # 4. Exact/substring product-name match against the shown options only.
        if not _picked_sku:
            for _cand_sku, _cand_prod in _pc_candidates:
                if _cand_prod and _cand_prod.name and _cand_prod.name.lower() in _stripped_pc_lower:
                    _picked_sku = _cand_sku
                    break

        # 5. Fuzzy name/keyword match (e.g. "kurti", "the cheap one" won't score
        # but "kurti best" will), scored ONLY against the shown candidates so
        # the match can never resolve to a product outside this choice. Only
        # accept when there's a single, unambiguous best match.
        if not _picked_sku and _pc_candidates:
            _pc_products_only = [p for _, p in _pc_candidates if p]
            if _pc_products_only:
                _pc_scored = catalogue_service.search_products_with_scores(_pc_products_only, _stripped_pc)
                if _pc_scored:
                    _pc_top_score, _pc_top_prod = _pc_scored[0]
                    _pc_second_score = _pc_scored[1][0] if len(_pc_scored) > 1 else 0
                    if _pc_top_score > _pc_second_score:
                        _picked_sku = getattr(_pc_top_prod, "sku", None)

        if _picked_sku:
            _picked_from = _pending_choice_skus_list
            try:
                await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _picked_sku)
                conv.pending_product_sku = _picked_sku
                await conversation_service.set_pending_choice_skus(db, conv.id, None)
                conv.pending_choice_skus = None
                await conversation_service.set_last_shown_sku(db, conv.id, _picked_sku)
                conv.last_shown_sku = _picked_sku
                if getattr(conv, "pending_choice_greeting_count", 0):
                    await conversation_service.update_order_field(
                        db, conv.id, "pending_choice_greeting_count", 0
                    )
                    conv.pending_choice_greeting_count = 0
                _pending_choice_skus_list = []
                out.pick_just_resolved = True
                logger.info(
                    "Multi-choice resolved: conv=%s picked=%s from %s",
                    conv.id, _picked_sku, _picked_from,
                )
                if message.type == "interactive":
                    logger.info(
                        "Button pick resolved by SKU conv=%s sku=%s",
                        conv.id, _picked_sku,
                    )
                # BUG 1 FIX: rebuild catalogue_context/canonical_browse_products for
                # the SKU just resolved from the multi-choice list — without this,
                # both stay at whatever _get_catalogue_context() resolved from the
                # customer's raw reply (e.g. "2"), which usually matches nothing or
                # an unrelated stale product. guard_product_reply() then checks the
                # FIX1 pinned-fact reply (built from the just-picked product) against
                # that wrong canonical set and flags the picked product's own
                # already-confirmed SKU/price as "phantom", same as the SKU-switch
                # fix above (see comment on canonical_browse_products near the
                # interrupted-SKU-switch branch).
                _picked_product = next(
                    (p for s, p in _pc_candidates if s == _picked_sku and p is not None), None
                )
                if _picked_product is not None:
                    out.catalogue_context = catalogue_service.format_catalogue_context(
                        [_picked_product], for_display=True
                    )
                    out.canonical_browse_products = [_picked_product]
            except Exception as exc:
                logger.error("Multi-choice resolve error: %s", exc)
        else:
            # No pick resolved. If the customer named an explicit, DIFFERENT
            # SKU (not one of the shown options), it's a deliberate switch —
            # let it fall through to the normal SKU-pin flow rather than
            # re-asking. Otherwise (bare "yes", an unrelated/ambiguous reply,
            # or a typo) the choice is still open: reject and re-ask rather
            # than guess.
            _foreign_skus = catalogue_service.extract_skus_from_text(user_text)
            _is_foreign_sku_ref = bool(_foreign_skus) and not any(
                s in _pending_choice_skus_list for s in _foreign_skus
            )
            if not _is_foreign_sku_ref and is_greeting_only(user_text):
                # BUG 2 FIX: a bare greeting ("Hi"/"Hello") while a choice list
                # is pending is not the same as a garbage/ambiguous reply —
                # verbatim-repeating the numbered list on every greeting gives
                # no acknowledgment and can loop indefinitely if the customer
                # keeps greeting. First greeting -> short warm re-ask instead
                # of the full list. If greetings keep coming (loop), give up
                # re-asking and fall back to open intent capture rather than
                # repeating the same list forever.
                _greeting_count = (getattr(conv, "pending_choice_greeting_count", 0) or 0) + 1
                try:
                    await conversation_service.update_order_field(
                        db, conv.id, "pending_choice_greeting_count", _greeting_count
                    )
                    conv.pending_choice_greeting_count = _greeting_count
                except Exception as exc:
                    logger.error("pending_choice_greeting_count update error: %s", exc)

                _GREETING_LOOP_BREAK_THRESHOLD = 2
                if _greeting_count >= _GREETING_LOOP_BREAK_THRESHOLD:
                    logger.info(
                        "Multi-choice open: %d consecutive greetings conv=%s — "
                        "clearing pending choice, falling back to open intent capture.",
                        _greeting_count, conv.id,
                    )
                    try:
                        await conversation_service.set_pending_choice_skus(db, conv.id, None)
                        conv.pending_choice_skus = None
                        await conversation_service.update_order_field(
                            db, conv.id, "pending_choice_greeting_count", 0
                        )
                        conv.pending_choice_greeting_count = 0
                    except Exception as exc:
                        logger.error("pending_choice_skus clear (greeting loop) error: %s", exc)
                    _pending_choice_skus_list = []
                    # Do NOT early-return — fall through so this greeting is
                    # handled by the normal browsing/greeting flow below,
                    # instead of being trapped re-asking the same list forever.
                else:
                    logger.info(
                        "Multi-choice open: bare greeting conv=%s (count=%d) — "
                        "short re-ask, not a verbatim list repeat.",
                        conv.id, _greeting_count,
                    )
                    _greeting_reask_msg = (
                        "Hey! Pick a number from the list above, or tell me "
                        "what you're looking for."
                    )
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _greeting_reask_msg)
                    except Exception:
                        pass
                    try:
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_greeting_reask_msg)
                    return out
            elif not _is_foreign_sku_ref:
                # Issue C: the reply may carry a variant (colour/size) even though
                # it didn't resolve to one of the shown products — e.g. "Green
                # xxl" answers a question we haven't asked yet. Stash it into the
                # normal slot columns now (instead of discarding it) so that once
                # the pick resolves on a later turn, the slot machine sees these
                # already filled and never re-asks for them.
                _PC_GENERIC_COLORS = (
                    "red", "blue", "green", "pink", "navy", "yellow", "white",
                    "black", "purple", "orange", "maroon", "gold", "silver",
                    "beige", "brown",
                )
                _PC_SIZE_TOKENS = {
                    "xs": "XS", "s": "S", "m": "M", "l": "L", "xl": "XL",
                    "xxl": "XXL", "xxxl": "XXXL",
                }
                _pc_text_lower = _stripped_pc_lower
                _pc_stash_color = next((c for c in _PC_GENERIC_COLORS if c in _pc_text_lower.split()), None)
                _pc_stash_size = None
                for _tok in _pc_text_lower.split():
                    _bare_tok = _tok.strip(".,!?")
                    if _bare_tok in _PC_SIZE_TOKENS:
                        _pc_stash_size = _PC_SIZE_TOKENS[_bare_tok]
                        break
                if _pc_stash_color and not getattr(conv, "selected_color", None):
                    try:
                        await conversation_service.update_order_field(db, conv.id, "selected_color", _pc_stash_color.title())
                        conv.selected_color = _pc_stash_color.title()
                    except Exception as _pcce:
                        logger.error("Multi-choice variant stash (color) error: %s", _pcce)
                if _pc_stash_size and not getattr(conv, "selected_size", None):
                    try:
                        await conversation_service.update_order_field(db, conv.id, "selected_size", _pc_stash_size)
                        conv.selected_size = _pc_stash_size
                    except Exception as _pcse:
                        logger.error("Multi-choice variant stash (size) error: %s", _pcse)
                if _pc_stash_color or _pc_stash_size:
                    logger.info(
                        "Multi-choice variant carry-forward: conv=%s color=%r size=%r — stashed, not discarded",
                        conv.id, _pc_stash_color, _pc_stash_size,
                    )

                # Before giving up on the reply as a bare affirmative/no-match,
                # check whether it actually names a DIFFERENT product than the
                # ones on offer — e.g. shown Kurti options, customer then types
                # "Saree". A genuinely ambiguous reply (empty, a generic
                # affirmative, or a number that didn't map to a choice above)
                # skips this check and falls through to the re-ask unchanged.
                _PC_GENERIC_AFFIRMATIVES = {
                    "yes", "yeah", "yep", "yup", "ok", "okay", "sure",
                    "haan", "ha", "theek hai",
                }
                _pc_is_ambiguous_reply = (
                    not _stripped_pc
                    or _stripped_pc_lower in _PC_GENERIC_AFFIRMATIVES
                    or _stripped_pc.isdigit()
                )
                _pc_new_query_prod = None
                _pc_match_kind = None
                _pc_fresh_confidence = 1.0
                if not _pc_is_ambiguous_reply and client:
                    # Explicit SKU/code mention always wins over the pending menu —
                    # e.g. "is SR99999 available?" is unambiguously a new-product
                    # lookup, even though search_products_with_scores below (which
                    # only scores name/category/description tokens) never matches a
                    # raw SKU and used to fall through to the bare-affirmative
                    # rejection, incorrectly re-showing the old menu.
                    for _pc_sku in catalogue_service.extract_skus_from_text(user_text):
                        try:
                            _pc_sku_prod = await catalogue_service.find_product_by_sku(db, client.id, _pc_sku)
                        except Exception:
                            _pc_sku_prod = None
                        if _pc_sku_prod is not None:
                            _pc_new_query_prod = _pc_sku_prod
                            _pc_match_kind = "sku"
                            break

                    if _pc_new_query_prod is None:
                        try:
                            _pc_all_prods = await catalogue_service.list_products(db, client.id)
                        except Exception:
                            _pc_all_prods = []
                        _pc_fresh_scored = catalogue_service.search_products_with_scores(
                            _pc_all_prods, user_text
                        )
                        if _pc_fresh_scored:
                            _pc_fresh_top_score, _pc_fresh_top_prod = _pc_fresh_scored[0]
                            _pc_fresh_keywords = catalogue_service._tokenize(user_text)
                            _pc_fresh_max_score = max(1, 2 * len(_pc_fresh_keywords))
                            _pc_fresh_confidence = _pc_fresh_top_score / _pc_fresh_max_score
                            _pc_fresh_sku = getattr(_pc_fresh_top_prod, "sku", None)
                            if (
                                _pc_fresh_confidence >= _NAME_MATCH_CONFIDENCE_FLOOR
                                and _pc_fresh_sku
                                and _pc_fresh_sku not in _pending_choice_skus_list
                            ):
                                _pc_new_query_prod = _pc_fresh_top_prod
                                _pc_match_kind = "name"

                if _pc_new_query_prod is not None:
                    logger.info(
                        "Multi-choice open: reply %r matches a different product "
                        "(sku=%s, match=%s, confidence=%.2f) not in pending choices %s — "
                        "clearing pending choice, treating as new catalog query.",
                        user_text[:40], getattr(_pc_new_query_prod, "sku", None),
                        _pc_match_kind, _pc_fresh_confidence, _pending_choice_skus_list,
                    )
                    try:
                        await conversation_service.set_pending_choice_skus(db, conv.id, None)
                        conv.pending_choice_skus = None
                        if getattr(conv, "pending_choice_greeting_count", 0):
                            await conversation_service.update_order_field(
                                db, conv.id, "pending_choice_greeting_count", 0
                            )
                            conv.pending_choice_greeting_count = 0
                    except Exception as exc:
                        logger.error("Multi-choice clear (new-query redirect) error: %s", exc)
                    _pending_choice_skus_list = []
                    # Do NOT return — fall through so the normal name-match/
                    # search flow below (which reruns its own fresh catalogue
                    # search) pins or multi-matches this query exactly as if
                    # no choice had been pending.
                else:
                    logger.info("Multi-choice open: rejecting bare affirmative, re-asking conv=%s", conv.id)
                    _reask_lines = []
                    for _cs, _cp in _pc_candidates:
                        _i = _pending_choice_skus_list.index(_cs) + 1
                        _cname = getattr(_cp, "name", None) or _cs
                        _cprice = int(getattr(_cp, "price", 0) or 0)
                        _reask_lines.append(f"{_i}. {_cname} [{_cs}] — ₹{_cprice:,}")
                    _reask_msg = (
                        "Please reply with the number of your choice:\n" + "\n".join(_reask_lines)
                    )
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _reask_msg)
                    except Exception:
                        pass
                    try:
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_reask_msg)
                    return out

    # ── P0-3: repin last_shown_sku on a bare affirmative after reset ─────────
    # After order completion/cancel, pending_product_sku is cleared but a new
    # product card may already be on screen (last_shown_sku). A bare "yes" in
    # a browsing stage with no product pinned must repin that SKU and enter
    # order_collection deterministically — it must NEVER be sent to the LLM,
    # which has no memory of what card was shown and replies "Which item?".
    # Guarded by "not _pending_choice_skus_list" — an open multi-choice list
    # must never be silently resolved by repinning a stale last_shown_sku.
    if (
        message.type == "text"
        and (conv.current_stage or "greeting") in conversation_flow._BROWSING_STAGES
        and not getattr(conv, "pending_product_sku", None)
        and getattr(conv, "last_shown_sku", None)
        and not _pending_choice_skus_list
        and user_text.strip().lower() in conversation_flow._PURCHASE_AFFIRMATION
    ):
        try:
            await conversation_service.update_order_field(db, conv.id, "pending_product_sku", conv.last_shown_sku)
            conv.pending_product_sku = conv.last_shown_sku
            out.p03_repinned = True
            logger.info(
                "P0-3 repin: conv=%s bare affirmative %r → pending_product_sku=%s (from last_shown_sku)",
                conv.id, user_text, conv.last_shown_sku,
            )
        except Exception as _repin_exc:
            logger.error("P0-3 repin error: %s", _repin_exc)

    # ── Resolve pinned product and its variant info ───────────────────────────
    # Placed HERE — after all new-SKU detection/reset logic above — so
    # variant_info always reflects conv.pending_product_sku as updated this turn.
    # Any later block that changes pending_product_sku (interrupted_sku switch,
    # pre-fill SKU parse) re-fetches pinned_product/variant_info inline.
    if client:
        pinned_sku = getattr(conv, "pending_product_sku", None)
        if pinned_sku:
            try:
                out.pinned_product = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
                if out.pinned_product:
                    out.variant_info = await catalogue_service.get_product_variant_info(db, out.pinned_product)
            except Exception as exc:
                logger.error("Variant info fetch error: %s", exc)

    # ── Post-completion variant recovery ─────────────────────────────────────
    # When stage=="completed" and the customer mentions a color/size/material
    # that belongs to the same product — without a new SKU — treat it as
    # wanting the same product in a different variant. Reset all order slots
    # (keep pending_product_sku), move to order_collection, restart from top.
    # Generic chat ("Hi", "thanks") contains no variant keywords → falls through.
    _post_completion_variant_reset = False
    if (
        message.type == "text"
        and user_text
        and (conv.current_stage or "greeting") == "completed"
        and getattr(conv, "pending_product_sku", None)
        and not catalogue_service.extract_skus_from_text(user_text)
        and client
    ):
        _cpl_vi = out.variant_info  # loaded above from pending_product_sku
        _msg_lower_cpl = user_text.lower()
        # Use word-boundary matching so single-letter sizes ("S", "M", "L") do not
        # match inside common words like "yes", "please", "thanks", "yes".
        _mentioned_color = next(
            (c for c in _cpl_vi.get("available_colors", [])
             if re.search(r'\b' + re.escape(c.lower()) + r'\b', _msg_lower_cpl)), None
        )
        _mentioned_size = next(
            (s for s in _cpl_vi.get("available_sizes", [])
             if re.search(r'\b' + re.escape(s.lower()) + r'\b', _msg_lower_cpl)), None
        )
        _mentioned_material = next(
            (m for m in _cpl_vi.get("available_materials", [])
             if re.search(r'\b' + re.escape(m.lower()) + r'\b', _msg_lower_cpl)), None
        )
        # Guard: bare affirmations ("yes", "haan", "ok", …) after a completed order are
        # NOT product-specific buy intent — they mean "yes, anything else?" or confirm
        # something conversational.  Never re-pin/restart from a bare affirmation.
        _BARE_AFFIRM_WORDS = frozenset({
            "yes", "haan", "ha", "han", "ok", "okay", "sure", "bilkul",
            "theek", "sahi", "zaroor", "yep", "yup", "y", "yeah",
        })
        _is_bare_affirm_post_completion = (
            user_text.lower().strip() in _BARE_AFFIRM_WORDS
        )
        if (_mentioned_color or _mentioned_size or _mentioned_material) and not _is_bare_affirm_post_completion:
            _cpl_reset_fields = [
                ("pending_order_quantity", None), ("selected_color", None),
                ("selected_size", None), ("selected_material", None),
                ("customer_name", None), ("delivery_address", None),
                ("payment_method", None), ("summary_shown", False),
            ]
            for _rf, _rv in _cpl_reset_fields:
                try:
                    await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                    setattr(conv, _rf, _rv)
                except Exception as exc:
                    logger.error("Post-completion variant reset (%s): %s", _rf, exc)
            try:
                await conversation_service.update_stage(db, conv.id, "order_collection")
                conv.current_stage = "order_collection"
            except Exception as exc:
                logger.error("Post-completion stage reset: %s", exc)
            _post_completion_variant_reset = True
            logger.info(
                "Post-completion variant restart: conv=%s SKU=%s "
                "color=%r size=%r material=%r",
                conv.id, conv.pending_product_sku,
                _mentioned_color, _mentioned_size, _mentioned_material,
            )

    # ── Post-completion generic chat → greeting ───────────────────────────────
    # "Hi", "thank you", etc. after a completed order are not variant requests.
    # Slide stage to greeting so the next real inquiry starts clean.
    _GENERIC_POST_COMPLETION_KW = frozenset({
        "hi", "hello", "hey", "namaste", "hii",
        "thank you", "thanks", "shukriya", "dhanyavaad", "ty",
        "bye", "goodbye", "alvida", "ok", "okay", "great", "nice",
        "madad chahiye", "help chahiye",
    })
    if (
        not _post_completion_variant_reset
        and (conv.current_stage or "greeting") == "completed"
        and set(user_text.lower().strip().split()) & _GENERIC_POST_COMPLETION_KW
    ):
        try:
            await conversation_service.update_stage(db, conv.id, "greeting")
            conv.current_stage = "greeting"
        except Exception as exc:
            logger.error("Post-completion greeting reset: %s", exc)
        logger.info("Post-completion generic chat — conv=%s stage reset to greeting", conv.id)

    # ── Improvement 4: Interrupted-SKU confirmation handler ─────────────────
    # When a low-confidence product switch was held for confirmation, handle
    # "yes" / "no" before any other name-match or SKU-pin logic runs.
    _interrupted_sku_pending = getattr(conv, "interrupted_sku", None)
    if (
        message.type == "text"
        and _interrupted_sku_pending
        and stored_stage not in ("order_collection", "awaiting_final_confirmation", "payment", "completed")
    ):
        _CONFIRM_YES = frozenset({"yes", "haan", "ha", "han", "ok", "okay", "sure", "y", "yep", "yeah", "bilkul", "हाँ", "ہاں"})
        _CONFIRM_NO = frozenset({"no", "nahi", "nope", "n", "nein", "na", "nah", "cancel"})
        _isku_txt = user_text.lower().strip()
        if _isku_txt in _CONFIRM_YES:
            _isku_prod = await catalogue_service.find_product_by_sku(db, client.id, _interrupted_sku_pending) if client else None
            if _isku_prod:
                _old_sku_isku = getattr(conv, "pending_product_sku", None)
                _isku_reset_fields = [
                    ("pending_order_quantity", None), ("selected_color", None),
                    ("selected_size", None), ("selected_material", None),
                    ("customer_name", None), ("delivery_address", None),
                    ("payment_method", None), ("summary_shown", False),
                ]
                for _rf, _rv in _isku_reset_fields:
                    try:
                        await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                        setattr(conv, _rf, _rv)
                    except Exception:
                        pass
                try:
                    await conversation_service.update_order_field(db, conv.id, "pending_product_sku", _interrupted_sku_pending)
                    conv.pending_product_sku = _interrupted_sku_pending
                    await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
                    conv.interrupted_sku = None
                    await conversation_service.update_stage(db, conv.id, "product_inquiry")
                    conv.current_stage = "product_inquiry"
                except Exception as _exc:
                    logger.error("Interrupted-SKU confirm switch error: %s", _exc)
                out.pinned_product = _isku_prod
                out.variant_info = await catalogue_service.get_product_variant_info(db, _isku_prod)
                out.catalogue_context = catalogue_service.format_catalogue_context([_isku_prod], for_display=True)
                out.canonical_browse_products = [_isku_prod]
                if _is_valid_image_url(getattr(_isku_prod, "image_url", None)):
                    pending_product_images.append((_isku_prod.image_url, f"{_isku_prod.name} — ₹{_isku_prod.price}"))
                logger.info(
                    "Interrupted-SKU confirmed: conv=%s switching %r → %r",
                    conv.id, _old_sku_isku, _interrupted_sku_pending,
                )
            # Let normal flow continue (will produce a product inquiry reply)
        elif _isku_txt in _CONFIRM_NO:
            try:
                await conversation_service.update_order_field(db, conv.id, "interrupted_sku", None)
                conv.interrupted_sku = None
            except Exception:
                pass
            logger.info("Interrupted-SKU declined: conv=%s keeping %r", conv.id, getattr(conv, "pending_product_sku", None))
            # Let normal flow continue

    # ── Name/fuzzy-search pinning (no SKU in message) ─────────────────────────
    # When the customer describes a product by name (e.g. "kanjivaram saree")
    # but doesn't include a SKU, run a scored keyword search. If there is a
    # single strong match, pin it exactly like an explicit SKU match so the
    # rest of the flow (T1: show details, ask to order) works identically.
    # Two or three close matches are surfaced to the AI without pinning (T10).
    _browsing_stage_check = stored_stage not in (
        "order_collection", "awaiting_final_confirmation", "payment", "completed"
    )
    # FIX 2: Never run the name-match pinner when an active order is in progress.
    # An "active order" means: a product is pinned AND at least one variant/order
    # slot has been answered — or the stored stage is already an order stage.
    # This prevents bare colour/size words ("pink", "XXL") from being treated as
    # a product search and resetting the slot machine mid-order.
    _stored_stage_is_order = stored_stage in (
        "order_collection", "awaiting_final_confirmation", "payment"
    )
    # NOTE: customer_name/delivery_address are deliberately excluded here.
    # _reset_order_slots_after_completion() preserves those two fields across
    # a completed order (so returning customers don't retype them), but that
    # means they stay set forever once a customer has ever ordered — including
    # cases where the customer is now just casually browsing again. Counting
    # them here would make _any_slot_filled (and therefore _active_order_context
    # below) permanently True for any returning customer, permanently disabling
    # the name-match pinner for the rest of the conversation.
    _any_slot_filled = bool(
        getattr(conv, "selected_color", None)
        or getattr(conv, "selected_size", None)
        or getattr(conv, "selected_material", None)
        or (getattr(conv, "pending_order_quantity", None) or 0)
    )
    _browsing_stage_pinned = stored_stage in (
        "product_inquiry", "qualification", "objection_handling", "offer_making", "greeting"
    )
    _is_bare_attribute = len(user_text.split()) == 1
    # A bare single word only counts as a plausible pending-slot answer
    # (colour/size/material/quantity) when it actually looks like one — a
    # digit, or a value that matches the pinned product's own variant
    # options. Previously ANY single word was assumed to be an attribute
    # answer, which meant a genuine new-product search typed as one word
    # (e.g. "Kurti") could never reach the name-match pinner below, so a
    # stale pinned SKU and its slot-filling state (stage=product_inquiry,
    # next_slot="color") never reset for an off-flow browse/search query.
    _is_plausible_bare_slot_answer = _is_bare_attribute and (
        user_text.strip().isdigit()
        or any(
            user_text.strip().lower() == v.lower()
            for v in (
                out.variant_info.get("available_colors", [])
                + out.variant_info.get("available_sizes", [])
                + out.variant_info.get("available_materials", [])
            )
        )
    )
    # P1-4: a multi-word variant answer ("Pink and xl") at offer stage must NOT be
    # treated as a new product search just because it isn't a single word — check
    # whether the message names a colour/size/material of the ALREADY-pinned product.
    _is_variant_answer_for_pinned = bool(
        getattr(conv, "pending_product_sku", None)
        and out.variant_info.get("has_variants")
        and any(
            re.search(r"\b" + re.escape(v.lower()) + r"\b", user_text.lower())
            for v in (
                out.variant_info.get("available_colors", [])
                + out.variant_info.get("available_sizes", [])
                + out.variant_info.get("available_materials", [])
            )
        )
    )
    _active_order_context = bool(
        getattr(conv, "pending_product_sku", None)
        and (
            _stored_stage_is_order
            or _any_slot_filled
            # Block re-pin for bare single-word attributes (color/material/size) when a
            # product is already pinned at a browsing stage. Multi-word messages (explicit
            # product names) are allowed through so a customer can switch products.
            or (_browsing_stage_pinned and _is_plausible_bare_slot_answer)
            # Block re-pin for a multi-word variant answer ("Pink and xl") naming an
            # attribute of the already-pinned product.
            or (_browsing_stage_pinned and _is_variant_answer_for_pinned)
        )
    )
    _no_sku_in_msg = not catalogue_service.extract_skus_from_text(user_text)
    if (
        message.type == "text"
        and user_text
        and _no_sku_in_msg
        and _browsing_stage_check
        and not _active_order_context  # FIX 2: skip when mid-order
        and client
    ):
        try:
            _all_prods = await catalogue_service.list_products(db, client.id)
            _scored = catalogue_service.search_products_with_scores(_all_prods, user_text)
            if _scored:
                _top_score, _top_prod = _scored[0]
                _second_score = _scored[1][0] if len(_scored) > 1 else 0
                # "Strong single match": only one result (with a non-trivial score —
                # a bare variant word like "Pink" can weakly score=1 against the
                # currently pinned product's name/description and must not count),
                # or top score ≥2× second.
                _single_strong = (
                    (len(_scored) == 1 and _top_score >= 2)
                    or (_top_score >= 2 * _second_score and _top_score >= 4)
                )
                # Normalized 0-1 confidence for ROUTE logging/tuning only — does
                # not gate _single_strong, which keeps its own raw-score logic.
                _match_confidence = min(1.0, _top_score / 10.0)
                # Issue D — minimum confidence floor: a weak overlap match (e.g. a
                # generic word incidentally shared with an unrelated product) must
                # never be offered as a confident pin. Confidence is the top score
                # relative to the max possible score for the query's own keyword
                # count (2 points per keyword on a full name match).
                _query_keywords = catalogue_service._tokenize(user_text)
                _max_possible_score = max(1, 2 * len(_query_keywords))
                _floor_confidence = _top_score / _max_possible_score
                if _single_strong and _floor_confidence < _NAME_MATCH_CONFIDENCE_FLOOR:
                    logger.info(
                        "Weak match below floor → not offering conv=%s query=%r score=%s",
                        conv.id, user_text[:80], _top_score,
                    )
                    _single_strong = False
                _log_route(
                    conv.id, "TEMPLATE" if _single_strong else "LLM",
                    "catalog_match_template" if _single_strong else "catalog_match_weak",
                    tier=1, match_score=_match_confidence,
                )
                if _single_strong:
                    _match_sku = getattr(_top_prod, "sku", None)
                    if _match_sku:
                        _old_name_sku = getattr(conv, "pending_product_sku", None)
                        # Improvement 4: confidence gate for product switching.
                        # If already pinned to a different product and score is below
                        # _NAME_MATCH_SWITCH_MIN_SCORE, send a confirmation question
                        # instead of silently switching.
                        _would_name_switch = bool(_old_name_sku and _match_sku != _old_name_sku)
                        # A unique match (no other catalogue product scored at all) is
                        # confident regardless of its absolute score — there is nothing
                        # else it could plausibly mean, so don't make the customer confirm.
                        _name_switch_confident = (
                            _top_score >= _NAME_MATCH_SWITCH_MIN_SCORE or len(_scored) == 1
                        )
                        if _would_name_switch and not _name_switch_confident:
                            _cand_price = int(getattr(_top_prod, "price", 0) or 0)
                            _confirm_question = (
                                f"Did you mean {_top_prod.name} (₹{_cand_price:,})? (yes/no)"
                            )
                            try:
                                await conversation_service.update_order_field(db, conv.id, "interrupted_sku", _match_sku)
                                conv.interrupted_sku = _match_sku
                            except Exception as _ice:
                                logger.error("Interrupted-SKU store error: %s", _ice)
                            logger.info(
                                "Name-match confidence gate: conv=%s score=%d < %d, holding switch %r→%r, asking confirm",
                                conv.id, _top_score, _NAME_MATCH_SWITCH_MIN_SCORE, _old_name_sku, _match_sku,
                            )
                            try:
                                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                                await conversation_service.save_message(db, conv.id, "assistant", _confirm_question)
                            except Exception:
                                pass
                            try:
                                await record_usage(db, client, conv)
                            except Exception:
                                pass
                            out.early_result = PipelineResult(text=_confirm_question)
                            return out
                        # Reset all order slots whenever a DIFFERENT product is identified by
                        # name. A no-op re-pin (same SKU matched again) must not wipe state —
                        # otherwise a bare variant word that re-matches the pinned product
                        # discards the colour/size the customer already gave (P1-4.2).
                        if _match_sku != _old_name_sku:
                            _name_reset_fields = [
                                ("pending_order_quantity", None), ("selected_color", None),
                                ("selected_size", None), ("selected_material", None),
                                ("customer_name", None), ("delivery_address", None),
                                ("payment_method", None), ("summary_shown", False),
                            ]
                            for _rf, _rv in _name_reset_fields:
                                try:
                                    await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                                    setattr(conv, _rf, _rv)
                                except Exception as exc:
                                    logger.error("Name-match slot reset (%s): %s", _rf, exc)
                        else:
                            logger.info(
                                "Name-match no-op re-pin: conv=%s SKU=%s — slots preserved.",
                                conv.id, _match_sku,
                            )
                        try:
                            await conversation_service.update_order_field(
                                db, conv.id, "pending_product_sku", _match_sku
                            )
                            conv.pending_product_sku = _match_sku
                        except Exception as exc:
                            logger.error("Name-match SKU pin error: %s", exc)
                        try:
                            await conversation_service.update_stage(db, conv.id, "product_inquiry")
                            conv.current_stage = "product_inquiry"
                        except Exception as exc:
                            logger.error("Name-match stage reset: %s", exc)
                        # Queue product image (sent after text reply — failure must not abort pin)
                        if _is_valid_image_url(getattr(_top_prod, "image_url", None)):
                            pending_product_images.append(
                                (_top_prod.image_url, f"{_top_prod.name} — ₹{_top_prod.price}")
                            )
                        # Rebuild catalogue_context so the AI sees only this product
                        out.catalogue_context = catalogue_service.format_catalogue_context(
                            [_top_prod], for_display=True
                        )
                        out.canonical_browse_products = [_top_prod]
                        logger.info(
                            "Name-match pin: conv=%s query=%r → SKU=%s (was %s, score=%d) — reset all order slots.",
                            conv.id, user_text[:40], _match_sku, _old_name_sku, _top_score,
                        )
                        # Re-fetch pinned_product and variant_info for the newly pinned SKU.
                        # The main fetch above ran before this block, so its
                        # results reflect the OLD sku.  We must update both here so all
                        # downstream slot-machine logic sees the correct product.
                        try:
                            out.pinned_product = _top_prod
                            out.variant_info = await catalogue_service.get_product_variant_info(db, _top_prod)
                            logger.info(
                                "Name-match variant_info refreshed: conv=%s SKU=%s has_variants=%s "
                                "needs_color=%s needs_size=%s",
                                conv.id, _match_sku,
                                out.variant_info.get("has_variants"),
                                out.variant_info.get("needs_color"),
                                out.variant_info.get("needs_size"),
                            )
                        except Exception as exc:
                            logger.error("Name-match variant_info re-fetch error: %s", exc)
                # FIX 3: 2+ close matches (any count, not just 2-3) — don't pin, but
                # rebuild context with just those products so the deterministic
                # multi-template list renders downstream instead of falling through
                # to open_browsing_no_match/70B (the count was previously capped at
                # 3, so a 4-saree match for "Is dress available?" never set
                # pending_choice_skus and silently fell through to the LLM).
                elif len(_scored) >= 2:
                    _match_prods = [p for _, p in _scored]
                    out.catalogue_context = catalogue_service.format_catalogue_context(
                        _match_prods, for_display=True
                    )
                    out.canonical_browse_products = _match_prods
                    out.multi_match_this_turn = True
                    # A "which one?" choice is now open. Record the shown SKUs so a
                    # bare affirmative is rejected/re-asked rather than repinned from
                    # last_shown_sku, and clear last_shown_sku since it no longer
                    # reflects what's on screen — nothing here was pinned/confirmed.
                    _choice_skus = [getattr(p, "sku", None) for p in _match_prods if getattr(p, "sku", None)]
                    try:
                        await conversation_service.set_pending_choice_skus(db, conv.id, _choice_skus)
                        conv.pending_choice_skus = _json.dumps(_choice_skus) if _choice_skus else None
                        await conversation_service.set_last_shown_sku(db, conv.id, None)
                        conv.last_shown_sku = None
                    except Exception as exc:
                        logger.error("Pending-choice-SKUs store error: %s", exc)
                    logger.info(
                        "Name-match multi (%d options): conv=%s query=%r — pending_choice_skus=%s",
                        len(_scored), conv.id, user_text[:40], _choice_skus,
                    )
        except Exception as exc:
            logger.warning("Name-match pinning failed (non-fatal): %s", exc)

    return out


# ---------------------------------------------------------------------------
# SLICE 4 — slot state machine (qty/variant/address/payment slot handling,
# next_slot computation, UPI-flow stage override, AFC-lock / negative-
# confirmation slot resets, SLOT-DEBUG logging)
# ---------------------------------------------------------------------------

@dataclass
class SlotOutcome:
    """
    Result of run_slot_state_machine().

    When early_result is set, the caller must (unless early_result.skip_send
    or early_result.text is falsy) send early_result.text — logging the send
    failure via `logger.error(early_send_error_label, exc)` when a label is
    set or swallowing it silently when not (mirrors the distinct inline
    send/except styles in the original code) — then return {"status": "ok"}
    without any further processing this turn.

    Otherwise the caller continues, splicing every field below back into its
    own locals in place of what it had before the call — exactly the same
    pattern as SkuPinOutcome (SLICE 3). `next_slot` and `current_instruction`
    are the two fields slice 5 (summary/confirmation rendering) is expected
    to consume directly; the rest are state the remaining ~1800 lines of
    reply-building logic in webhook.py still reads.
    """

    early_result: "PipelineResult | None" = None
    early_send_error_label: str | None = None
    stage: str = "greeting"
    pinned_product: object = None
    variant_info: dict = field(default_factory=lambda: {
        "has_variants": False, "needs_color": False, "needs_size": False,
        "needs_material": False, "available_colors": [], "available_sizes": [],
        "available_materials": [],
    })
    available_stock: int | None = None
    next_slot: str | None = None
    current_instruction: str = ""
    quantity_invalid: bool = False
    combo_oos: bool = False
    declined_saved_address: bool = False
    is_first_slot: bool = False
    confirmation_gate_fired: bool = False
    intent_override: str | None = None
    classified_intent: str | None = None
    switch_resolved: bool = False
    afc_crosssell_switched: bool = False
    crosssell_sku_for_session: str | None = None


# Sentinel field names returned by conversation_flow.extract_order_field()
# for the Phase 1 cart engine — none of these are real Conversation columns,
# so they must never reach the generic single-column setattr() path used for
# every other extracted field. _apply_cart_extraction() below is the only
# thing that writes them.
_CART_SENTINEL_FIELDS = frozenset({
    "cart_multi_seed", "cart_variant_mode", "cart_wip_color", "cart_wip_size",
    "cart_wip_material", "cart_wip_qty", "cart_wip_qty_invalid",
    "cart_breakdown_confirmed", "cart_breakdown_rejected", "cart_correct_last_qty",
})


async def _apply_cart_extraction(db, conv, variant_info: dict, pinned_product, field: str, value) -> bool:
    """
    Persist one cart-sentinel extraction result from extract_order_field().

    Mirrors the write-then-setattr pattern conversation_service.update_order_field
    uses for ordinary slots, but every cart sentinel maps to a JSONB column
    (cart_items/cart_wip_item/cart_pending_confirmation) or derives a second
    column (cart_collection_mode) rather than a single 1:1 column write, so
    each needs its own handling instead of the generic path.

    Returns True on a value the customer should be told is invalid (so the
    caller can drive the same slot_attempt_count increment/re-ask behavior
    used for an ordinary write-rejected value) — False otherwise.
    """
    async def _set(f: str, v) -> None:
        await conversation_service.update_order_field(db, conv.id, f, v)
        setattr(conv, f, v)

    sku = getattr(pinned_product, "sku", None)
    name = getattr(pinned_product, "name", None)
    price = getattr(pinned_product, "price", 0)

    if field == "cart_multi_seed":
        # value = [{"color": str, "qty": int}, ...] — 2+ distinct colors
        # named with quantities in one message ("10 red and 10 pink").
        _total = sum(p["qty"] for p in value)
        _existing_qty = getattr(conv, "pending_order_quantity", None)
        if _existing_qty and _existing_qty != _total:
            # A total was already stated and disagrees with the pairs' sum —
            # never silently override one with the other.
            logger.warning(
                "cart_multi_seed mismatch: conv=%s pairs_sum=%d vs stated_qty=%d — rejecting.",
                conv.id, _total, _existing_qty,
            )
            return True
        _items = [
            {
                "sku": sku, "product_name": name, "color": p["color"],
                "size": getattr(conv, "selected_size", None),
                "material": getattr(conv, "selected_material", None),
                "qty": p["qty"], "unit_price": price,
            }
            for p in value
        ]
        await _set("pending_order_quantity", _total)
        await _set("cart_variant_mode", "different")
        _remaining_after_seed = _total - sum(i["qty"] for i in _items)
        await _set(
            "cart_collection_mode",
            "batch" if _remaining_after_seed > conversation_flow._CART_BATCH_THRESHOLD else "loop",
        )
        await _set("cart_items", _items)
        logger.info("Multi-color capture: conv=%s items=%r", conv.id, _items)
        return False

    if field == "cart_variant_mode":
        if value == "different":
            _n = getattr(conv, "pending_order_quantity", 0) or 0
            # Line item 1 = the color/size/material already collected before
            # this question was asked — its own qty is still unknown, so the
            # "remaining" count used to pick loop vs batch mode is N minus
            # nothing yet (item 1 hasn't been asked its qty at this point).
            await _set("cart_collection_mode", "batch" if _n > conversation_flow._CART_BATCH_THRESHOLD else "loop")
        else:
            await _set("cart_collection_mode", None)
        await _set("cart_variant_mode", value)
        return False

    if field in ("cart_wip_color", "cart_wip_size", "cart_wip_material", "cart_wip_qty"):
        _subkey = {
            "cart_wip_color": "color", "cart_wip_size": "size",
            "cart_wip_material": "material", "cart_wip_qty": "qty",
        }[field]
        if _subkey == "color":
            _valid = [c.lower() for c in variant_info.get("available_colors", [])]
            if _valid and str(value).lower() not in _valid:
                logger.warning("WRITE REJECTED (cart) conv=%s field=%r value=%r", conv.id, field, value)
                return True
        elif _subkey == "size":
            _valid = [s.lower() for s in variant_info.get("available_sizes", [])]
            if _valid and str(value).lower() not in _valid:
                logger.warning("WRITE REJECTED (cart) conv=%s field=%r value=%r", conv.id, field, value)
                return True
        elif _subkey == "material":
            _valid = [m.lower() for m in variant_info.get("available_materials", [])]
            if _valid and str(value).lower() not in _valid:
                logger.warning("WRITE REJECTED (cart) conv=%s field=%r value=%r", conv.id, field, value)
                return True

        _existing_items = getattr(conv, "cart_items", None) or []
        if field == "cart_wip_qty" and not _existing_items:
            # Line item 1 — its color/size/material were already collected by
            # the leading slots (conv.selected_color/size/material) before the
            # "same or different" question was ever asked, never written into
            # cart_wip_item. Build the completed item straight from those
            # instead of reading a cart_wip_item that was never populated.
            _items = [{
                "sku": sku, "product_name": name,
                "color": getattr(conv, "selected_color", None),
                "size": getattr(conv, "selected_size", None),
                "material": getattr(conv, "selected_material", None),
                "qty": value, "unit_price": price,
            }]
            await _set("cart_items", _items)
            logger.info("Cart line item 1 committed: conv=%s item=%r", conv.id, _items[0])
            return False

        _wip = dict(getattr(conv, "cart_wip_item", None) or {})
        _wip[_subkey] = value
        _complete = (
            (not variant_info.get("needs_color") or _wip.get("color"))
            and (not variant_info.get("needs_size") or _wip.get("size"))
            and (not variant_info.get("needs_material") or _wip.get("material"))
            and _wip.get("qty")
        )
        if _complete:
            _items = list(getattr(conv, "cart_items", None) or [])
            _items.append({
                "sku": sku, "product_name": name,
                "color": _wip.get("color"), "size": _wip.get("size"), "material": _wip.get("material"),
                "qty": _wip["qty"], "unit_price": price,
            })
            await _set("cart_items", _items)
            await _set("cart_wip_item", None)
            logger.info("Cart line item committed: conv=%s item=%r", conv.id, _items[-1])
        else:
            await _set("cart_wip_item", _wip)
        return False

    if field == "cart_wip_qty_invalid":
        # value = remaining pieces still to assign — the customer's number
        # didn't fit (either <1 or more than what's left to allocate).
        logger.warning("WRITE REJECTED (cart) conv=%s field=cart_item_qty remaining=%r", conv.id, value)
        return True

    if field == "cart_breakdown_confirmed":
        # An LLM-inferred split was echoed back and the customer confirmed it.
        _pending = getattr(conv, "cart_pending_confirmation", None) or []
        _items = list(getattr(conv, "cart_items", None) or []) + _pending
        await _set("cart_items", _items)
        await _set("cart_pending_confirmation", None)
        logger.info("Cart breakdown confirmed: conv=%s items_added=%d", conv.id, len(_pending))
        return False

    if field == "cart_breakdown_rejected":
        await _set("cart_pending_confirmation", None)
        logger.info("Cart breakdown rejected by customer: conv=%s — will re-ask.", conv.id)
        return False

    if field == "cart_correct_last_qty":
        # Mid-loop correction ("actually make it 60 not 40") — adjusts the
        # LAST committed cart line item's qty in place, never creates a
        # duplicate. Rejected (re-ask) if the new total would exceed N.
        _items = list(getattr(conv, "cart_items", None) or [])
        if not _items:
            return True
        _n = getattr(conv, "pending_order_quantity", 0) or 0
        _others_sum = sum(i["qty"] for i in _items[:-1])
        if value < 1 or _others_sum + value > _n:
            logger.warning(
                "WRITE REJECTED (cart) conv=%s field=cart_correct_last_qty value=%r "
                "(others=%d, N=%d)", conv.id, value, _others_sum, _n,
            )
            return True
        _items[-1] = {**_items[-1], "qty": value}
        await _set("cart_items", _items)
        logger.info("Cart line item corrected: conv=%s item=%r", conv.id, _items[-1])
        return False

    logger.error("_apply_cart_extraction: unrecognised cart sentinel field=%r conv=%s", field, conv.id)
    return True


async def run_slot_state_machine(
    db,
    conv,
    client,
    message,
    user_text: str,
    sender_phone: str,
    wamid: str | None,
    language: str,
    history_dicts: list,
    stage: str,
    _stored_stage: str,
    pinned_product,
    variant_info: dict,
    record_usage,
) -> SlotOutcome:
    """
    stage: the stage value computed by detect_stage()/buy-intent-gate/stage-
    lock/payment-keyword-guard upstream (still inline in webhook.py — those
    concerns are NOT part of this slice). This function only applies the
    UPI-flow override, the pre-filled-catalogue-order parse, available_stock/
    OOS-combo computation, the AFC negative-confirmation slot resets, and the
    order_collection slot-extraction/next_slot machinery on top of it.

    _stored_stage: the conversation's stage snapshotted BEFORE this turn's
    mutations (webhook.py's `_stored_stage`) — gates the UPI override and the
    interrupted-SKU-style branches on the pre-update value, exactly as in the
    original inline code. Mirrors how run_sku_and_name_pinning (SLICE 3) took
    stored_stage as an explicit parameter rather than re-deriving it.

    pinned_product / variant_info: the values produced by SLICE 3
    (run_sku_and_name_pinning), passed in explicitly rather than re-read from
    conv — this function may itself reassign both (UPI prefill parse,
    cross-sell switch, NEW_PRODUCT auto-switch) and returns the final values
    on SlotOutcome for the caller to splice back, same shape as SLICE 3.

    record_usage: the webhook module's `_record_usage` callable, passed in
    rather than imported since it still lives in webhook.py.

    Mirrors webhook.py's original inline slot-machine block verbatim (DB
    writes, message saves, logging, the seven early-return paths) — moved
    here unchanged as part of the strangler-fig extraction. `conv` is
    mutated in place via direct attribute sets, exactly as in the original
    inline code.
    """
    out = SlotOutcome(stage=stage, pinned_product=pinned_product, variant_info=variant_info)
    customer_profile = None

    # ── Channel-conditional mobile_number fill ────────────────────────────────
    # mobile_number is delivery data, not identity. On WhatsApp the sender IS
    # a phone number, so auto-fill it and never ask — the slot loop
    # (get_next_required_slot) then skips straight past it. On Instagram the
    # sender is an IGSID, not a phone, so it stays empty here and is collected
    # as a normal slot later in this function, same as delivery_address.
    if conv.channel == "whatsapp" and not getattr(conv, "mobile_number", None):
        # Mutate in-memory only — `conv` is already attached to this request's
        # session, so the value rides along with whichever commit the rest of
        # the pipeline naturally performs next. An eager standalone commit()
        # here (re-selecting + committing on every single turn, including
        # non-order stages) caused a flush race with later writes in the same
        # request ("0 rows matched" PendingRollbackError) — see replay suite.
        conv.mobile_number = sender_phone

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

    # ── Simple-product OOS gate: no variants, so unlike the combo gate above
    # there is no attribute left to clear and re-pick — the product itself is
    # sold out. Fires right when the product is confirmed for order (the "Yes"
    # that put us in order_collection), BEFORE quantity is ever asked. Without
    # this, get_next_required_slot() would return "quantity" and the customer
    # would eventually be asked "How many would you like (1–0)?" — a
    # nonsensical range.
    if (
        pinned_product
        and not getattr(pinned_product, "has_variants", False)
        and available_stock is not None
        and available_stock <= 0
        and stage == "order_collection"
    ):
        _oos_name = getattr(pinned_product, "name", None) or "This product"
        _oos_lang = (getattr(conv, "last_customer_language", None) or "english").lower()
        _oos_reply = get_template(_oos_lang, "out_of_stock_block", product=_oos_name)
        logger.info(
            "Product OOS gate fired at order confirm: conv=%s SKU=%s stock=%s — resetting order slots",
            conv.id, getattr(pinned_product, "sku", None), available_stock,
        )
        await _reset_flow_state(db, conv)
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _oos_reply)
        except Exception as exc:
            logger.error("Product OOS save error: %s", exc)
        try:
            await record_usage(db, client, conv)
        except Exception:
            pass
        out.early_result = PipelineResult(text=_oos_reply)
        return out

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

    # Phase 1 cart engine — batch-mode breakdown parsing needs an LLM call
    # (extract_order_field can't award since it's sync), so it's handled
    # directly in the ANSWER branch below rather than through the normal
    # deterministic extractor. These flags override _next_slot the same way
    # _quantity_invalid does, so the existing _build_slot_question dispatch
    # renders the right re-ask with no separate call site needed.
    _cart_breakdown_mismatch = False
    _cart_breakdown_invalid = False

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
                            # Phase 1 cart engine: "Add X too" builds ONE cart —
                            # fold the just-finished selection into cart_items
                            # (as another line item) instead of wiping it, and
                            # keep customer_name/delivery_address/payment_method
                            # since this is still the same order, same delivery.
                            _cs_existing_cart = list(getattr(conv, "cart_items", None) or [])
                            if not _cs_existing_cart:
                                _cs_old_qty = getattr(conv, "pending_order_quantity", None)
                                if _cs_old_qty:
                                    _cs_existing_cart.append({
                                        "sku": _current_sku_cs2,
                                        "product_name": getattr(pinned_product, "name", _current_sku_cs2),
                                        "color": getattr(conv, "selected_color", None),
                                        "size": getattr(conv, "selected_size", None),
                                        "material": getattr(conv, "selected_material", None),
                                        "qty": _cs_old_qty,
                                        "unit_price": getattr(pinned_product, "price", 0),
                                    })
                            try:
                                await conversation_service.update_order_field(db, conv.id, "cart_items", _cs_existing_cart)
                                conv.cart_items = _cs_existing_cart
                            except Exception as _cs_cart_e:
                                logger.error("Cross-sell cart_items fold error: %s", _cs_cart_e)

                            # Reset per-product working slots for the new product only.
                            _cs_reset_fields = [
                                ("pending_product_sku", _new_cs_sku),
                                ("pending_order_quantity", None),
                                ("selected_color", None),
                                ("selected_size", None),
                                ("selected_material", None),
                                ("summary_shown", False),
                                ("cart_variant_mode", None),
                                ("cart_collection_mode", None),
                                ("cart_wip_item", None),
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
                ("cart_items", None), ("cart_variant_mode", None),
                ("cart_collection_mode", None), ("cart_wip_item", None),
                ("cart_pending_confirmation", None),
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
                await record_usage(db, client, conv)
            except Exception:
                pass
            out.early_result = PipelineResult(text=_cancel_reply)
            out.early_send_error_label = "AFC cancel send error: %s"
            return out
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
            # Set True below when the address-confirmation gate accepts the
            # saved address — skips FIX 3's separate "change address" keyword
            # scan, which has no negation handling and would otherwise
            # re-trigger on phrases like "I do not want to change the address".
            _address_confirmed_this_turn = False
            # ── Address-confirmation gate ───────────────────────────────────
            # When the agent just offered the customer's saved address
            # ("{name}, deliver to: {address}? (yes/change)"), classify the
            # reply deterministically (Tier 0 — no LLM, no address validation)
            # BEFORE anything else touches this turn. Previously "yes"/"Hi"
            # fell through to address extraction/validation (wrong: "doesn't
            # look like a complete address") and "I do not want to change the
            # address" tripped a bare "change" keyword match (inverted
            # intent). This must run ahead of the slot-attempt cap and FIX 3
            # change-address detection so neither can misfire on this reply.
            if (
                message.type == "text"
                and _next_slot_pre == "delivery_address"
                and customer_profile is not None
                and getattr(customer_profile, "address", None)
                and conversation_flow._last_agent_offered_saved_address(history_dicts)
            ):
                _saved_addr_for_confirm = customer_profile.address
                _ac_classification = conversation_flow.classify_address_confirmation(user_text)
                if _ac_classification == "confirm":
                    try:
                        await conversation_service.update_order_field(
                            db, conv.id, "delivery_address", _saved_addr_for_confirm
                        )
                        conv.delivery_address = _saved_addr_for_confirm
                    except Exception as _ac_exc:
                        logger.error("Address-confirm update error: %s", _ac_exc)
                    _address_confirmed_this_turn = True
                    # delivery_address is now filled — recompute _next_slot_pre so
                    # the rest of this turn (slot-attempt tracking, extraction,
                    # the "no value extracted for delivery_address" rejection
                    # branch) operates on the NEW current slot instead of the
                    # stale "delivery_address", which would otherwise wrongly
                    # reject this very reply as an invalid address.
                    _next_slot_pre = conversation_flow.get_next_required_slot(conv, variant_info)
                    # Fall through — normal flow recomputes next_slot (payment /
                    # summary) below; do not return early.
                elif _ac_classification == "change":
                    _ac_ask = "Sure — what's the new delivery address? Please send house/area, city and pincode."
                    try:
                        await conversation_service.update_order_field(db, conv.id, "delivery_address", None)
                        conv.delivery_address = None
                        await conversation_service.update_order_field(db, conv.id, "summary_shown", False)
                        conv.summary_shown = False
                        # Customer explicitly chose to retype — don't let stale
                        # unclear-reply attempts from THIS gate carry over.
                        await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", 0)
                        conv.slot_attempt_count = 0
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _ac_ask)
                    except Exception as _ac_clr_exc:
                        logger.error("Address-confirm change error: %s", _ac_clr_exc)
                    try:
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_ac_ask)
                    return out
                else:  # "unclear" — e.g. "Hi"
                    # Same slot_attempt_count used for delivery_address elsewhere —
                    # without this, unclear replies at this gate never advance the
                    # cap and the customer can loop here forever.
                    _ac_new_attempts = (conv.slot_attempt_count or 0) + 1
                    conv.slot_attempt_count = _ac_new_attempts
                    try:
                        await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", _ac_new_attempts)
                    except Exception as _ac_inc_exc:
                        logger.error("slot_attempt address-confirm-unclear increment: %s", _ac_inc_exc)
                    logger.info(
                        "Slot attempt (address-confirm unclear): conv=%s slot=delivery_address attempt=%d",
                        conv.id, _ac_new_attempts,
                    )
                    _ac_lang_cap = getattr(conv, "last_customer_language", None) or language or "english"
                    if _ac_new_attempts >= _SLOT_ATTEMPT_ESCALATE:
                        # Escalate exactly like the slot-cap path below (Improvement 1):
                        # stop re-prompting, hand off to a human.
                        if _ac_lang_cap in ("hindi_roman", "hindi_devanagari", "hinglish"):
                            _ac_reprompt = "Hamara team aapki madad karega. 👋\n\nPlease reply *yes* to use this address, or *change* to enter a new one."
                        elif _ac_lang_cap in ("gujarati_roman", "gujarati_script"):
                            _ac_reprompt = "Amari team tamne madad karse. 👋\n\nPlease reply *yes* to use this address, or *change* to enter a new one."
                        else:
                            _ac_reprompt = "Our team will assist you shortly. 👋\n\nPlease reply *yes* to use this address, or *change* to enter a new one."
                        logger.warning(
                            "SLOT cap: conv=%s slot=delivery_address attempts=%d — no LLM, escalating to human (address-confirm gate).",
                            conv.id, _ac_new_attempts,
                        )
                        try:
                            from app.models.conversation import Conversation as _ConvAcCap
                            from sqlalchemy import select as _selAcCap
                            _ac_cr = await db.execute(_selAcCap(_ConvAcCap).where(_ConvAcCap.id == conv.id).limit(1))
                            _ac_cobj = _ac_cr.scalar_one_or_none()
                            if _ac_cobj:
                                _ac_cobj.ai_enabled = False
                                _ac_cobj.taken_over_at = datetime.utcnow()
                                _ac_cobj.taken_over_note = f"Slot cap: delivery_address x{_ac_new_attempts}"
                                await db.commit()
                        except Exception as _ac_esc_exc:
                            logger.error("Address-confirm slot-cap escalation error: %s", _ac_esc_exc)
                    else:
                        _ac_reprompt = "Please reply *yes* to use this address, or *change* to enter a new one."
                        if _ac_new_attempts >= _SLOT_ATTEMPT_ESCAPE_HATCH:
                            _ac_reprompt += "\n\n(Reply 'cancel' to stop, or 'help' to reach our team.)"
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _ac_reprompt)
                    except Exception:
                        pass
                    try:
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_ac_reprompt)
                    return out

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
                    await record_usage(db, client, conv)
                except Exception:
                    pass
                out.early_result = PipelineResult(text=_cap_reply)
                return out

            # ── FIX 3: Change-address intent mid-order ────────────────────────
            _ca_detected, _ca_addr = (
                (False, None) if _address_confirmed_this_turn
                else _detect_change_address_intent(user_text)
            )
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
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_ca_ask)
                    return out

            # ── FIX 2 / FIX 4: Aside-question — answer + re-ask slot ─────────
            # Intercept side-questions (delivery charges, price of another product,
            # etc.) BEFORE intent classification so they are never treated as slot
            # answers and the current slot is re-asked after the one-line answer.
            if message.type == "text" and _is_order_aside_question(user_text):
                _aq_lang = getattr(conv, "last_customer_language", None) or language or "english"
                _aq_prod_name = getattr(pinned_product, "name", "the product") or "the product"
                _aq_dt = get_delivery_time_str(pinned_product, client) or "3–7 business days"
                _aq_answer = ""

                # FIX 4 (moved ahead of the availability-question tier below):
                # detect whether the question NAMES a different product than the
                # one pinned for order — e.g. "Kanjivaram Silk Saree available in
                # red?" while a chair is pinned. This lookup must run BEFORE the
                # availability tier, not just as its fallback: previously an
                # availability question always answered from the PINNED
                # product's variants regardless of what product the text named,
                # because that tier ran first and almost always produced *some*
                # answer — so this cross-product lookup never got a chance to
                # fire for availability questions at all. Pending order state
                # (pinned SKU, slots) is never touched here — only the answer
                # text changes; the slot re-ask below still targets the pinned
                # order via the unmodified `variant_info`/`pinned_product`.
                _aq_pinned_sku = getattr(conv, "pending_product_sku", None)
                _aq_named_product = None
                _aq_named_not_found_label = None
                if client:
                    try:
                        for _aq_named_sku in catalogue_service.extract_skus_from_text(user_text):
                            if _aq_named_sku == _aq_pinned_sku:
                                continue
                            _aq_sku_match = await catalogue_service.find_product_by_sku(db, client.id, _aq_named_sku)
                            if _aq_sku_match is not None:
                                _aq_named_product = _aq_sku_match
                                break
                            # An explicit SKU-shaped code was named but doesn't
                            # resolve — remember it in case no other token in
                            # the message resolves to a real product either.
                            _aq_named_not_found_label = _aq_named_sku
                        if _aq_named_product is None:
                            _aq_all_prods = await catalogue_service.list_products(db, client.id)
                            _aq_scored = catalogue_service.search_products_with_scores(
                                _aq_all_prods, user_text, top_k=3
                            )
                            for _aq_sc, _aq_cp in _aq_scored:
                                if _aq_sc >= 3 and getattr(_aq_cp, "sku", None) != _aq_pinned_sku:
                                    _aq_named_product = _aq_cp
                                    _aq_named_not_found_label = None
                                    break
                    except Exception as _aqe:
                        logger.warning("FIX4 cross-product lookup failed: %s", _aqe)

                if _aq_named_product is None and _aq_named_not_found_label:
                    # An explicit product code was named but isn't in the
                    # catalogue — say so directly rather than silently
                    # answering about the pinned product instead.
                    _aq_answer = f"Sorry, we don't carry {_aq_named_not_found_label}."
                    logger.info(
                        "FIX4 named product not in catalogue: conv=%s code=%r — answering directly, not pinned SKU",
                        conv.id, _aq_named_not_found_label,
                    )

                # BUG 1 FIX: availability questions ("is orange available?") are
                # resolved against the CATALOG / current product's variants, never
                # the KB — KB keyword overlap on a word like "available" was
                # returning unrelated FAQs (e.g. a dispatch/tracking entry).
                # When the question named a DIFFERENT product than the one
                # pinned for order (found above), answer about THAT product's
                # own variants/stock instead of the pinned product's.
                if not _aq_answer and _is_availability_question(user_text):
                    if _aq_named_product is not None:
                        _aq_named_variant_info = await catalogue_service.get_product_variant_info(db, _aq_named_product)
                        _aq_named_stock = (
                            None if getattr(_aq_named_product, "has_variants", False)
                            else (getattr(_aq_named_product, "stock", None) or 0)
                        )
                        _aq_answer = _build_availability_answer(
                            user_text, _aq_named_product, _aq_named_variant_info, _aq_named_stock,
                        )
                        if _aq_answer:
                            logger.info(
                                "FIX2 availability question: conv=%s answered about named product sku=%s "
                                "(pinned=%s), skipping KB",
                                conv.id, getattr(_aq_named_product, "sku", None), _aq_pinned_sku,
                            )
                    else:
                        _aq_answer = _build_availability_answer(
                            user_text, pinned_product, variant_info, available_stock,
                        )
                        if _aq_answer:
                            logger.info(
                                "FIX2 availability question: conv=%s answered from catalog/variants, skipping KB",
                                conv.id,
                            )

                # FIX 4 cont'd: a confident named-product match on a
                # non-availability question (price, "do you have X?", etc.) is
                # more specific than the generic pinned-product answer below.
                if not _aq_answer and _aq_named_product is not None:
                    _aq_answer = (
                        f"{_aq_named_product.name} — ₹{int(getattr(_aq_named_product, 'price', 0) or 0):,}."
                    )

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

                if _aq_answer and _aq_named_product is not None:
                    # The answer above was about a DIFFERENT, named product than
                    # the one pinned for this order. Re-asking the OLD pinned
                    # slot here (even tagged "(for <pinned product>)") reads as
                    # a non-sequitur — the customer just asked about the named
                    # product, not the pinned one. Offer to switch the order to
                    # it instead; the ORIGINAL pinned slot is only re-asked if
                    # the customer declines (see run_order_switch_confirm_guard,
                    # which resolves this on the next turn).
                    _aq_switch_prompt = f"Would you like to order {_aq_named_product.name}? (Yes / No)"
                    _aq_reply = f"{_aq_answer}\n\n{_aq_switch_prompt}"
                    try:
                        await conversation_service.update_order_field(
                            db, conv.id, "interrupted_sku", _aq_named_product.sku
                        )
                        conv.interrupted_sku = _aq_named_product.sku
                        await conversation_service.update_stage(db, conv.id, "awaiting_order_switch_confirm")
                        conv.current_stage = "awaiting_order_switch_confirm"
                    except Exception as _aq_stash_exc:
                        logger.error("FIX2/FIX4 switch-offer stash error: %s", _aq_stash_exc)
                    logger.info(
                        "FIX4 cross-product aside: conv=%s named=%s pinned=%s — offering switch instead of "
                        "re-asking pinned slot %r",
                        conv.id, getattr(_aq_named_product, "sku", None), _aq_pinned_sku, _next_slot_pre,
                    )
                    try:
                        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                        await conversation_service.save_message(db, conv.id, "assistant", _aq_reply)
                    except Exception:
                        pass
                    try:
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_aq_reply)
                    return out
                elif _aq_answer:
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
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_aq_reply)
                    return out
                # No deterministic answer — fall through to normal intent classification.

            # ── Smart greeting during active slot-filling ──────────────────────
            # A pure greeting ("hi"/"hello"/"namaste"/…) with nothing else mixed
            # in must never be treated as a slot-answer attempt: extract_order_field
            # has no way to parse "hi" as a size/color/qty/address, so without this
            # check it fell through to the same "no value extracted" rejection path
            # as a genuinely bad answer — "Sorry, we don't have hi in size." —
            # burning an attempt on a customer who was just saying hello.
            # is_greeting_only() requires the ENTIRE message to be nothing but the
            # greeting (not a substring match), so "hi, is chair available in
            # blue?" is correctly NOT a pure greeting here and falls through to
            # the FIX2/FIX4 aside-question handling above (it already matched,
            # since it contains "?") instead of ever reaching this branch.
            if message.type == "text" and is_greeting_only(user_text):
                _greet_lang = getattr(conv, "last_customer_language", None) or language or "english"
                # Same name-lookup precedence as the greeting_short_circuit route's
                # personalized welcome: prefer this order's captured name, fall
                # back to the saved customer profile (returning customer).
                _greet_name = getattr(conv, "customer_name", None) or getattr(customer_profile, "name", None)
                _GREET_WELCOME = {
                    "english": f"Hey {_greet_name}! Welcome back 👋" if _greet_name else "Hey! Welcome back 👋",
                    "hindi": f"Hey {_greet_name}! Vapas aane ke liye shukriya 👋" if _greet_name else "Hey! Vapas aane ke liye shukriya 👋",
                    "gujarati": f"Hey {_greet_name}! Pacha aavva badal aabhar 👋" if _greet_name else "Hey! Pacha aavva badal aabhar 👋",
                }
                _GREET_BACK = {
                    "english": "Let's finish your order now 🙂",
                    "hindi": "Chaliye aapka order poora karte hain 🙂",
                    "gujarati": "Chalo, tamaru order pooru karie 🙂",
                }
                if _greet_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                    _greet_welcome = _GREET_WELCOME["hindi"]
                    _greet_reply = _GREET_BACK["hindi"]
                elif _greet_lang in ("gujarati_roman", "gujarati_script"):
                    _greet_welcome = _GREET_WELCOME["gujarati"]
                    _greet_reply = _GREET_BACK["gujarati"]
                else:
                    _greet_welcome = _GREET_WELCOME["english"]
                    _greet_reply = _GREET_BACK["english"]
                _greet_prod_name = getattr(pinned_product, "name", "the product") or "the product"
                _greet_slot_q = _build_slot_question(
                    _next_slot_pre, conv, variant_info, _greet_lang,
                    customer_profile=customer_profile,
                    accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                    available_stock=available_stock,
                    product_name=_greet_prod_name,
                    # Capped at 0: _build_slot_question appends a "(Reply
                    # 'cancel'... )" escape hatch once attempt_count >=
                    # _SLOT_ATTEMPT_ESCAPE_HATCH (3). A friendly re-greet after
                    # earlier failed slot attempts must not carry that nudge —
                    # it reads as a warning right after "Let's finish your
                    # order now 🙂". The real attempt count is untouched in
                    # conv/DB; this only affects what this one re-ask renders.
                    attempt_count=0,
                )
                _greet_msg2 = f"{_greet_reply}\n{_greet_slot_q}" if _greet_slot_q else _greet_reply
                logger.info(
                    "Pure greeting mid-slot: conv=%s slot=%r — welcome + (finish-order+re-ask) as two messages, "
                    "no state change, no attempt increment",
                    conv.id, _next_slot_pre,
                )
                try:
                    await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                    await conversation_service.save_message(
                        db, conv.id, "assistant",
                        f"{_greet_welcome}\n\n{_greet_msg2}",
                    )
                except Exception:
                    pass
                try:
                    await record_usage(db, client, conv)
                except Exception:
                    pass
                out.early_result = PipelineResult(
                    text=_greet_msg2,
                    pre_texts=[_greet_welcome],
                )
                return out

            # ── Intent classification ──────────────────────────────────────────
            _product_name = getattr(pinned_product, "name", None) or getattr(conv, "pending_product_sku", "current product") or "current product"

            # Just-entered-order_collection guard: detect_stage() (upstream,
            # before this function runs) transitions browsing → order_collection
            # on a bare purchase affirmation ("yes") after the AI offered to
            # sell ("Would you like to order? (Yes/No)"). That SAME "yes" was
            # then being fed into the P0-2/LLM classify block below as if it
            # were an attempt to answer the first slot question (e.g. size) —
            # it obviously isn't a colour/size/material value, so extraction
            # failed and the customer got "Sorry, we don't have yes in size."
            # in an unbreakable loop. "yes" already did its job (the stage
            # transition); it carries no further content to extract. Route
            # straight to OTHER so resolve_transition() just asks the first
            # slot question cleanly, without treating "yes" as a bad answer.
            _just_entered_order_collection_via_affirmation = (
                _stored_stage in conversation_flow._BROWSING_STAGES
                and stage == "order_collection"
                and user_text.strip().lower() in conversation_flow._PURCHASE_AFFIRMATION
            )

            # P0-2: deterministic fast-path — a trivial slot answer (color/size/
            # qty/yes-no/free-text address) never needs an LLM classify call.
            # Skip straight to ANSWER + extraction when the message:
            #   - contains no price-objection / cancel keywords
            #   - is not a bare SKU token belonging to a DIFFERENT product
            #   - cleanly resolves via the deterministic extractor for the
            #     current slot
            _det_skip_llm = False
            if _just_entered_order_collection_via_affirmation:
                pass
            elif (
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
                elif _next_slot_pre == "cart_breakdown":
                    # Phase 1 cart engine — extract_order_field() deliberately
                    # returns None for "cart_breakdown" (parsing needs an LLM
                    # await, which this sync probe can't do), so it never
                    # trips the check above. The keyword guard on this whole
                    # `if` block already ruled out cancel/price-objection
                    # wording, so anything left is a breakdown answer — skip
                    # straight to ANSWER same as any other deterministic slot.
                    _det_skip_llm = True

            if _just_entered_order_collection_via_affirmation:
                _intent = "OTHER"
                logger.info(
                    "conv=%s bare affirmation %r just triggered browsing→order_collection — "
                    "OTHER (ask first slot), not treated as an answer to next_slot=%s",
                    conv.id, user_text[:40], _next_slot_pre,
                )
                _intent_struct = {"intent": "OTHER", "entities": {}}
            elif _det_skip_llm:
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
                    elif _switch_product is None:
                        # _new_sku_candidate is a SKU-SHAPED token pulled straight out of
                        # the customer's raw text via regex (extract_skus_from_text) — it
                        # is NOT validated against the catalogue yet at this point. Without
                        # this branch, the `else:` below used to run for a not-found SKU
                        # the same as a found one, blindly pinning pending_product_sku to a
                        # candidate that doesn't exist in the DB (a typo, or the customer
                        # echoing back a hallucinated code from an earlier bad reply) —
                        # exactly the kind of phantom-product write that let a fake order
                        # reach payment confirmation. Never pin an unresolved SKU; just
                        # re-ask the current slot instead.
                        logger.info(
                            "conv=%s NEW_PRODUCT switch ignored — SKU %r not found in catalogue.",
                            conv.id, _new_sku_candidate,
                        )
                        _intent_override = "OTHER"
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
                        logger.info(
                            "event=product_type_changed conv=%s old_sku=%s new_sku=%s",
                            conv.id, _old_pinned, _new_sku_candidate,
                        )
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
                    ("cart_items", None), ("cart_variant_mode", None),
                    ("cart_collection_mode", None), ("cart_wip_item", None),
                    ("cart_pending_confirmation", None),
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
                        await record_usage(db, client, conv)
                    except Exception:
                        pass
                    out.early_result = PipelineResult(text=_ot_boundary_mo)
                    return out
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
                    await record_usage(db, client, conv)
                except Exception:
                    pass
                out.early_result = PipelineResult(text=_ot_reply)
                out.early_send_error_label = "OFF_TOPIC mid-order send error: %s"
                return out

            elif _intent == "OTHER":
                # Skip extraction — AI answers the question, then slot re-ask is appended.
                _intent_override = "OTHER"  # sentinel for the reply-building section
                logger.info("conv=%s OTHER — skipping extraction, will re-ask slot", conv.id)

            elif _next_slot_pre == "cart_breakdown" and message.type == "text":
                # Phase 1 cart engine, batch mode — free-text variant breakdown
                # needs an LLM parse. Never guesses: an unparseable reply just
                # re-asks the same question (extracted stays unset below), a
                # sum mismatch or unknown variant reprompts with the specific
                # problem, and an LLM-inferred split is always echoed back for
                # confirmation before being committed to cart_items.
                _cb_n = getattr(conv, "pending_order_quantity", 0) or 0
                _cb_remaining = _cb_n - conversation_flow._cart_committed_qty(conv)
                _cb_result = await conversation_flow.extract_cart_breakdown(
                    user_text, variant_info, _product_name, _cb_remaining, conversation_id=conv.id,
                )
                if _cb_result is not None:
                    _cb_items = _cb_result["items"]
                    _cb_valid_colors = {c.lower() for c in variant_info.get("available_colors", [])}
                    _cb_valid_sizes = {s.lower() for s in variant_info.get("available_sizes", [])}
                    _cb_invalid = [
                        i for i in _cb_items
                        if (i.get("color") and i["color"].lower() not in _cb_valid_colors)
                        or (i.get("size") and i["size"].lower() not in _cb_valid_sizes)
                    ]
                    if _cb_invalid:
                        _bad = ", ".join(str(i.get("color") or i.get("size")) for i in _cb_invalid)
                        conv.__dict__["_cart_breakdown_invalid_info"] = {
                            "invalid": _bad,
                            "colors": " / ".join(variant_info.get("available_colors", [])) or "see catalogue",
                            "sizes": " / ".join(variant_info.get("available_sizes", [])) or "see catalogue",
                        }
                        _cart_breakdown_invalid = True
                        logger.info("cart_breakdown invalid variant conv=%s bad=%r", conv.id, _bad)
                    else:
                        _cb_sum = sum(i["qty"] for i in _cb_items)
                        _cb_line_items = [
                            {
                                "sku": getattr(pinned_product, "sku", None),
                                "product_name": getattr(pinned_product, "name", None),
                                "color": i.get("color"), "size": i.get("size"), "material": None,
                                "qty": i["qty"], "unit_price": getattr(pinned_product, "price", 0),
                            }
                            for i in _cb_items
                        ]
                        if _cb_result["inferred_split"]:
                            # ALWAYS echo an inferred split back — never commit
                            # it silently, regardless of whether the sum happens
                            # to already match.
                            try:
                                await conversation_service.update_order_field(
                                    db, conv.id, "cart_pending_confirmation", _cb_line_items
                                )
                                conv.cart_pending_confirmation = _cb_line_items
                                logger.info("cart_breakdown inferred split staged for confirm: conv=%s items=%r", conv.id, _cb_line_items)
                            except Exception as exc:
                                logger.error("cart_pending_confirmation write error: %s", exc)
                        elif _cb_sum != _cb_remaining:
                            conv.__dict__["_cart_breakdown_mismatch_info"] = {
                                "sum": _cb_sum, "remaining": _cb_remaining,
                            }
                            _cart_breakdown_mismatch = True
                            logger.info(
                                "cart_breakdown mismatch conv=%s sum=%d remaining=%d — reprompting.",
                                conv.id, _cb_sum, _cb_remaining,
                            )
                        else:
                            try:
                                _cb_all_items = list(getattr(conv, "cart_items", None) or []) + _cb_line_items
                                await conversation_service.update_order_field(db, conv.id, "cart_items", _cb_all_items)
                                conv.cart_items = _cb_all_items
                                logger.info("cart_breakdown committed: conv=%s items_added=%d", conv.id, len(_cb_line_items))
                            except Exception as exc:
                                logger.error("cart_items batch-commit write error: %s", exc)
                # _cb_result is None → LLM parse failure; nothing is written,
                # so next_slot naturally stays "cart_breakdown" and the same
                # question is re-asked below — no guess, no silent commit.

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
                    if field in _CART_SENTINEL_FIELDS:
                        # Phase 1 cart engine — none of these are real columns,
                        # so they get their own write path entirely, never the
                        # generic single-column setattr() below.
                        _cart_write_rejected = await _apply_cart_extraction(
                            db, conv, variant_info, pinned_product, field, value
                        )
                        if _cart_write_rejected:
                            _new_wr = (conv.slot_attempt_count or 0) + 1
                            conv.slot_attempt_count = _new_wr
                            try:
                                await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", _new_wr)
                            except Exception as _wri:
                                logger.error("slot_attempt write-rejected increment (cart): %s", _wri)
                        else:
                            try:
                                await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", 0)
                                await conversation_service.update_order_field(db, conv.id, "slot_attempt_slot", None)
                                await conversation_service.update_order_field(db, conv.id, "off_topic_count", 0)
                                conv.slot_attempt_count = 0
                                conv.slot_attempt_slot = None
                                conv.off_topic_count = 0
                            except Exception as _slr:
                                logger.error("Slot-fill counter reset error (cart): %s", _slr)
                    elif field == "quantity_invalid":
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
                            await record_usage(db, client, conv)
                        except Exception:
                            pass
                        out.early_result = PipelineResult(text=_addr_rej)
                        return out

                    # Variant slots: say WHY the answer was rejected instead of
                    # echoing the identical question. Live symptom: customer
                    # answered "45" to "Size? 38 / 3XL / 40 / L / M / S / XL /
                    # XXL" and got the same line back with no hint that 45
                    # isn't stocked. Mirrors the address-rejection path above.
                    if (
                        _next_slot_pre in ("color", "size", "material")
                        and message.type == "text"
                        and len(user_text.split()) <= 3
                    ):
                        _vs_lang = getattr(conv, "last_customer_language", None) or language or "english"
                        _vs_opts = " / ".join(
                            (variant_info or {}).get(
                                {
                                    "color": "available_colors",
                                    "size": "available_sizes",
                                    "material": "available_materials",
                                }[_next_slot_pre],
                                [],
                            )
                        )
                        if _vs_opts:
                            _VS_REJ = {
                                "english": "Sorry, we don't have {value} in {slot}. Available {slot}s: {opts}",
                                "hindi": "Maafi, {value} {slot} available nahi hai. Available {slot}: {opts}",
                                "gujarati": "Maaf karo, {value} {slot} available nathi. Available {slot}: {opts}",
                            }
                            if _vs_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                                _vs_tpl = _VS_REJ["hindi"]
                            elif _vs_lang in ("gujarati_roman", "gujarati_script"):
                                _vs_tpl = _VS_REJ["gujarati"]
                            else:
                                _vs_tpl = _VS_REJ["english"]
                            _vs_rej = _vs_tpl.format(
                                value=user_text.strip()[:20],
                                slot=_next_slot_pre,
                                opts=_vs_opts,
                            )
                            logger.info(
                                "Variant-slot rejection: conv=%s slot=%r value=%r attempt=%d",
                                conv.id, _next_slot_pre, user_text[:20], _new_noext,
                            )
                            try:
                                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                                await conversation_service.save_message(db, conv.id, "assistant", _vs_rej)
                            except Exception:
                                pass
                            try:
                                await record_usage(db, client, conv)
                            except Exception:
                                pass
                            out.early_result = PipelineResult(text=_vs_rej)
                            return out
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

    # Phase 1 cart engine — fold a completed "Add {product} too" cross-sell
    # selection into cart_items. That handler already moved the FIRST
    # product's data into cart_items and reset the flat fields for the new
    # (second) product; if the second product never entered "different"
    # mode (qty=1, or "same" for all N), its own data still lives only in
    # the flat fields and must be folded in here — otherwise it would be
    # silently dropped from the final order the moment cart_items renders/
    # creates from cart_items alone (never happens for cart_variant_mode ==
    # "different", since item 1 there is folded the instant its own qty is
    # answered — see _apply_cart_extraction).
    if (
        _next_slot is None
        and (getattr(conv, "cart_items", None) or [])
        and getattr(conv, "cart_variant_mode", None) is None
        and getattr(conv, "pending_order_quantity", None)
    ):
        try:
            _fold_items = list(conv.cart_items)
            _fold_items.append({
                "sku": getattr(conv, "pending_product_sku", None),
                "product_name": getattr(pinned_product, "name", None),
                "color": getattr(conv, "selected_color", None),
                "size": getattr(conv, "selected_size", None),
                "material": getattr(conv, "selected_material", None),
                "qty": conv.pending_order_quantity,
                "unit_price": getattr(pinned_product, "price", 0),
            })
            await conversation_service.update_order_field(db, conv.id, "cart_items", _fold_items)
            conv.cart_items = _fold_items
            logger.info("Cross-sell product folded into cart_items: conv=%s item=%r", conv.id, _fold_items[-1])
        except Exception as exc:
            logger.error("Cross-sell fold-into-cart_items error: %s", exc)

    # When quantity was invalid we stay in order_collection but tell the AI
    # to re-ask with the stock limit rather than asking the next slot.
    if _quantity_invalid:
        _next_slot = "quantity_invalid"

    # When a variant combo is OOS, override next_slot to re-ask the cleared attr.
    if _combo_oos:
        _next_slot = "combo_oos"

    # Phase 1 cart engine — batch-breakdown sum mismatch / unknown variant.
    if _cart_breakdown_mismatch:
        _next_slot = "cart_breakdown_mismatch"
    if _cart_breakdown_invalid:
        _next_slot = "cart_breakdown_invalid"

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

    out.stage = stage
    out.pinned_product = pinned_product
    out.variant_info = variant_info
    out.available_stock = available_stock
    out.next_slot = _next_slot
    out.current_instruction = _current_instruction
    out.quantity_invalid = _quantity_invalid
    out.combo_oos = _combo_oos
    out.declined_saved_address = _declined_saved_address
    out.is_first_slot = _is_first_slot
    out.confirmation_gate_fired = _confirmation_gate_fired
    out.intent_override = _intent_override
    out.classified_intent = _classified_intent
    out.switch_resolved = _switch_resolved
    out.afc_crosssell_switched = _afc_crosssell_switched
    out.crosssell_sku_for_session = _crosssell_sku_for_session
    return out


async def _find_cross_sell_candidate(
    db, client, conv, pinned_sku: str | None
) -> tuple[str | None, str | None]:
    """
    Return (name, formatted_price) for the most-recently-browsed SKU other
    than the currently-pinned one, or (None, None) if there is no candidate.

    Reads conv.browsed_skus (JSON list appended to on every product
    switch/pin) and looks up the last entry that differs from pinned_sku.
    """
    if not client:
        return None, None
    import json as _json_cs

    try:
        _browsed = _json_cs.loads(getattr(conv, "browsed_skus", None) or "[]")
    except Exception:
        _browsed = []
    _others = [s for s in _browsed if s != pinned_sku]
    if not _others:
        return None, None
    try:
        _cs_p = await catalogue_service.find_product_by_sku(db, client.id, _others[-1])
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("cross-sell lookup failed: %s", exc)
        return None, None
    if _cs_p and getattr(_cs_p, "name", None):
        return _cs_p.name, format_price(getattr(_cs_p, "price", 0) or 0)
    return None, None


# ---------------------------------------------------------------------------
# SLICE 5 — order-summary text construction + confirm-prompt rendering
# (relocated _render_order_reply, formerly webhook.py's single render
# dispatch function, moved here verbatim — no behavior change)
# ---------------------------------------------------------------------------

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
    # Phase 1 cart engine — for a "different for each" cart, pending_order_
    # quantity is the TARGET TOTAL N, not any one line item's own qty, and
    # selected_color/selected_size below only ever hold line item 1's data
    # (item 2+ live in cart_items). qty * unit_price only happens to equal
    # the real total when every line item shares the same unit price — true
    # today (Phase 1 carts are single-product/multi-variant only) but not a
    # safe assumption to bake into every render action. Compute the grand
    # total from cart_items directly whenever it's populated so every
    # action below (show_payment, confirm_paid_*, ...) gets the real
    # figure regardless of per-variant pricing.
    _cart_for_total = getattr(conv, "cart_items", None) or []
    if _cart_for_total:
        _total = int(sum(i["qty"] * i["unit_price"] for i in _cart_for_total))
    else:
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

        # Phase 1 cart engine — a "different for each" order has 2+ line
        # items in conv.cart_items. The "same for all" path never touches
        # cart_items, so it falls straight through to the single-item logic
        # below, completely unchanged.
        _cart = getattr(conv, "cart_items", None) or []
        if _cart:
            if any(i.get("unit_price") is None for i in _cart):
                raise RenderError(
                    f"show_summary(cart): a line item is missing unit_price (conv={conv.id})"
                )
            _cart_pay_display = _pay
            if _pay and _pay.upper() == "UPI":
                _cart_upi = getattr(client, "upi_id", None) if client else None
                if _cart_upi:
                    _cart_pay_display = f"{_pay} ({_cart_upi})"
            _cart_lines = []
            _cart_grand_total = 0.0
            for _item in _cart:
                _sub = _item["qty"] * _item["unit_price"]
                _cart_grand_total += _sub
                _variant_bits = " ".join(p for p in [_item.get("color"), _item.get("size")] if p)
                _cart_lines.append(
                    f"📦 {_item['product_name']} {_variant_bits} × {_item['qty']} = {format_price(_sub)}"
                )
            _cart_delivery = get_delivery_time_str(_prod, client) or "3–7 business days"
            return _gt(
                lang, "cart_order_summary",
                items_block="\n".join(_cart_lines),
                total=format_price(_cart_grand_total),
                name=_name, address=_addr, payment=_cart_pay_display,
                delivery_time=_cart_delivery,
            )
        # Show the actual UPI ID alongside the payment method so the customer
        # doesn't have to wait for the separate payment-instructions message
        # to know where to send money.
        _pay_display = _pay
        if _pay and _pay.upper() == "UPI":
            _upi_for_summary = getattr(client, "upi_id", None) if client else None
            if _upi_for_summary:
                _pay_display = f"{_pay} ({_upi_for_summary})"
        _summary_delivery = get_delivery_time_str(_prod, client) or "3–7 business days"
        _total_fmt = format_price(_total)
        # Cross-sell: most-recently-browsed different SKU
        _cs_name: str | None = None
        _cs_price_fmt: str | None = None
        if not getattr(conv, "summary_shown", False):
            _cs_name, _cs_price_fmt = await _find_cross_sell_candidate(db, client, conv, _pinned_sku)
        if _cs_name and _cs_price_fmt:
            _tpl = "order_summary_variant_crosssell" if _variant_str else "order_summary_crosssell"
            _kw: dict = dict(product=_prod_name, qty=_qty, total=_total_fmt,
                             name=_name, address=_addr, payment=_pay_display,
                             delivery_time=_summary_delivery,
                             cs_name=_cs_name, cs_price=_cs_price_fmt)
        else:
            _tpl = "order_summary_variant" if _variant_str else "order_summary"
            _kw = dict(product=_prod_name, qty=_qty, total=_total_fmt,
                       name=_name, address=_addr, payment=_pay_display,
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
        _cs_name_r, _cs_price_r = await _find_cross_sell_candidate(db, client, conv, _pinned_sku)
        if _cs_name_r and _cs_price_r and _cat_url_r:
            _catalogue_line = f"🛍️ You might also like {_cs_name_r} ({_cs_price_r}) — {_cat_url_r}"
        elif _cat_url_r:
            _catalogue_line = f"🛍️ Browse more: {_cat_url_r}"
        else:
            _catalogue_line = ""
        _total_fmt = format_price(_total)

        # Phase 1 cart engine — a "different for each" order has 2+ line
        # items in conv.cart_items (item 1 is folded in the instant its own
        # qty is known, item 2+ as each is committed — see
        # _apply_cart_extraction). This render fires BEFORE run_order_payment
        # resets cart_items post-completion, so it's still the live cart for
        # THIS order. Using the flat product/variant/qty columns here would
        # show only line item 1 and a merged quantity — exactly the bug this
        # branch fixes. The "same" path never touches cart_items and falls
        # through to the single-item render below, unchanged.
        _cart_r = getattr(conv, "cart_items", None) or []
        if _cart_r:
            if any(i.get("unit_price") is None for i in _cart_r):
                raise RenderError(
                    f"{action}(cart): a line item is missing unit_price (conv={conv.id})"
                )
            _cart_r_lines = []
            _cart_r_total = 0.0
            for _item in _cart_r:
                _sub = _item["qty"] * _item["unit_price"]
                _cart_r_total += _sub
                _variant_bits = " ".join(p for p in [_item.get("color"), _item.get("size")] if p)
                _cart_r_lines.append(
                    f"📦 {_item['product_name']} {_variant_bits} × {_item['qty']} = {format_price(_sub)}"
                )
            _cart_tpl = "order_confirmed_cod_cart" if action == "confirm_paid_cod" else "order_confirmed_paid_cart"
            return _gt(
                lang, _cart_tpl,
                items_block="\n".join(_cart_r_lines),
                total=format_price(_cart_r_total),
                delivery_time=_delivery_time_r,
                catalogue_line=_catalogue_line,
            )

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



# ---------------------------------------------------------------------------
# SLICE 5 — summary / final-confirmation orchestration for
# stage == "awaiting_final_confirmation" (downstream of SLICE 4's SlotOutcome)
# ---------------------------------------------------------------------------

@dataclass
class SummaryOutcome:
    """
    Result of run_summary_confirmation().

    Only ever called by webhook.py when stage == "awaiting_final_confirmation"
    (the order_collection / payment / completed branches stay inline in
    webhook.py — out of scope for this slice).

    When early_result is set, the caller must (unless early_result.skip_send
    or early_result.text is falsy) send early_result.text — silently
    swallowing any send failure (mirrors the original inline
    `except Exception: pass` for this one early-return path; no
    early_send_error_label field is needed since the original code never
    logged this particular send failure) — then return {"status": "ok"}
    without any further processing this turn.

    When render_error is True, the caller must return {"status":
    "render_error"} immediately — the RenderError has already been logged
    inside this function with the exact original message, so the caller
    must NOT log it again.

    Otherwise (the normal show_summary/reask_confirm render), `text` holds
    the channel-neutral reply and `render_action` holds the resolved action
    key (always "show_summary" in practice for this stage, computed the same
    way as the original inline code) — both are what slices 7-8 are expected
    to consume next: slice 7 (order creation/payment) decides whether to
    create the Order row from here, and slice 8 decides button/list dispatch
    from `render_action`/the rendered text, exactly as webhook.py's existing
    button-type selection already does today.
    """

    early_result: "PipelineResult | None" = None
    render_error: bool = False
    text: str | None = None
    render_action: str | None = None


async def run_summary_confirmation(
    db,
    conv,
    client,
    message,
    user_text: str,
    wamid: str | None,
    stage: str,
    lang: str,
    next_slot: str | None,
    variant_info: dict,
    customer_profile,
    available_stock: int | None,
    declined_saved_address: bool,
    is_first_slot: bool,
    intent_override: str | None,
    classified_intent: str | None,
    pinned_product,
    record_usage,
) -> SummaryOutcome:
    """
    stage: must be "awaiting_final_confirmation" — the value SLICE 4's
    SlotOutcome.stage produced for this turn, passed in explicitly (never
    re-derived from conv.current_stage, which still holds the PREVIOUS
    turn's stage at this point).

    next_slot / declined_saved_address / is_first_slot: taken verbatim from
    SLICE 4's SlotOutcome — by the time stage == "awaiting_final_confirmation"
    reaches this function, SLICE 4's confirmation-gate guarantees next_slot
    is always None, but it is still threaded explicitly rather than
    recomputed, exactly like stored_stage in SLICE 3 / stage in SLICE 4.

    intent_override / classified_intent: also taken verbatim from SLICE 4's
    SlotOutcome — both are always None for this stage in practice (SLICE 4
    only populates them inside its own `stage == "order_collection"`
    branch), but the original eff_intent computation is preserved exactly
    rather than hardcoded, in case a future SLICE 4 change ever populates
    them for this stage too.

    record_usage: the webhook module's `_record_usage` callable, passed in
    rather than imported, same as SLICE 3 / SLICE 4.

    Mirrors webhook.py's original inline AFC-aside-question early-return and
    the AFC half of the shared order_collection/AFC render dispatch verbatim
    (DB writes, message saves, logging) — moved here unchanged as part of
    the strangler-fig extraction. `conv` is mutated in place via direct
    attribute sets (summary_shown), exactly as in the original inline code.
    """
    out = SummaryOutcome()

    if (
        stage == "awaiting_final_confirmation"
        and next_slot is None
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
                from app.config import get_settings as _afc_get_settings
                _afc_settings = _afc_get_settings()
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
                if getattr(_afc_resp, "usage", None) is not None:
                    cost_log.log(
                        conv.id, "IN", user_text,
                        path="LLM", model="llama-3.1-8b-instant",
                        in_tok=_afc_resp.usage.prompt_tokens,
                        out_tok=_afc_resp.usage.completion_tokens,
                        call_kind="reply",
                    )
                _afc_q_answer = (_afc_resp.choices[0].message.content or "").strip()
            except Exception as _afc_llm_exc:
                logger.warning("AFC cheap-model fallback failed: %s", _afc_llm_exc)
                _afc_q_answer = "Let me check that for you."
        try:
            _afc_summary = await _render_order_reply(
                action="show_summary",
                conv=conv, db=db, client=client,
                next_slot=next_slot, variant_info=variant_info,
                customer_profile=customer_profile,
                available_stock=available_stock,
                declined_saved_address=declined_saved_address,
                lang=lang, is_first_slot=is_first_slot,
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
            await record_usage(db, client, conv)
        except Exception:
            pass
        out.early_result = PipelineResult(text=ai_reply)
        return out

    # ── Free-text product switch at the confirm step ─────────────────────
    # detect_stage() only ever keeps stage=="awaiting_final_confirmation" for
    # replies that are NOT the 1/2/3 button words (those already resolved to
    # "completed"/"order_collection" before this function is ever called —
    # see conversation_flow.detect_stage) and NOT an aside question (handled
    # above). So anything reaching here is free text the customer typed
    # instead of tapping a button — e.g. a product code ("SR27754") to swap
    # the item mid-confirmation. Previously this fell straight into the
    # SLOTS_DONE→show_summary branch below and silently re-sent the exact
    # same summary verbatim, with no acknowledgement of what the customer
    # typed. Try a catalog match first; only fall back to the dumb re-show
    # when nothing matches.
    #
    # Guarded on conv.current_stage (still the PREVIOUS turn's stage here,
    # per this function's docstring) == "awaiting_final_confirmation": this
    # must only fire when the customer was already sitting at a
    # previously-shown summary. When slot-filling completes THIS turn (SLICE
    # 4 bumps stage → AFC same-turn while conv.current_stage is still
    # "order_collection"), the raw slot answer (e.g. "COD") must never be
    # run through the catalog-switch heuristic — that turn owns rendering
    # the summary for the first time.
    if (
        stage == "awaiting_final_confirmation"
        and conv.current_stage == "awaiting_final_confirmation"
        and message.type == "text"
        and client
        and user_text.strip()
    ):
        _afc_switch_product = None
        _afc_mentioned_skus = catalogue_service.extract_skus_from_text(user_text)
        if _afc_mentioned_skus:
            _afc_switch_product = await catalogue_service.find_product_by_sku(
                db, client.id, _afc_mentioned_skus[0]
            )
        if _afc_switch_product is None:
            try:
                _afc_all_products = await catalogue_service.list_products(db, client.id)
                _afc_scored = catalogue_service.search_products_with_scores(
                    _afc_all_products, user_text, top_k=1
                )
            except Exception as _afc_cat_exc:
                logger.warning("AFC catalog switch lookup failed: %s", _afc_cat_exc)
                _afc_scored = []
            if _afc_scored and _afc_scored[0][0] >= _NAME_MATCH_SWITCH_MIN_SCORE:
                _afc_switch_product = _afc_scored[0][1]

        _afc_old_sku = getattr(conv, "pending_product_sku", None)
        if _afc_switch_product is not None and getattr(_afc_switch_product, "sku", None) != _afc_old_sku:
            # Single active draft (no multi-item cart): overwrite the pinned
            # product and reset the variant/qty slots so the customer re-fills
            # them for the new item. Name/address/payment already collected
            # are kept — only the product-specific slots depend on the item.
            _afc_switch_reset_fields = [
                ("pending_order_quantity", None), ("selected_color", None),
                ("selected_size", None), ("selected_material", None),
                ("summary_shown", False),
            ]
            for _rf, _rv in _afc_switch_reset_fields:
                try:
                    await conversation_service.update_order_field(db, conv.id, _rf, _rv)
                    setattr(conv, _rf, _rv)
                except Exception as exc:
                    logger.error("AFC switch slot reset (%s): %s", _rf, exc)
            try:
                await conversation_service.update_order_field(
                    db, conv.id, "pending_product_sku", _afc_switch_product.sku
                )
                conv.pending_product_sku = _afc_switch_product.sku
                await conversation_service.update_stage(db, conv.id, "order_collection")
                conv.current_stage = "order_collection"
            except Exception as exc:
                logger.error("AFC switch pin error: %s", exc)
            _afc_new_variant_info: dict = {}
            try:
                _afc_new_variant_info = await catalogue_service.get_product_variant_info(db, _afc_switch_product)
            except Exception as exc:
                logger.error("AFC switch variant_info fetch: %s", exc)
            logger.info(
                "conv=%s AFC free-text switch %r → %r", conv.id, _afc_old_sku, _afc_switch_product.sku,
            )
            _afc_new_next_slot = conversation_flow.get_next_required_slot(conv, _afc_new_variant_info)
            _afc_switch_sq = _build_slot_question(
                _afc_new_next_slot, conv, _afc_new_variant_info, lang,
                customer_profile=customer_profile,
                accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                available_stock=None,  # deferred: variant attrs not yet re-chosen
                product_name=_afc_switch_product.name,
            )
            ai_reply = f"Sure, switching to {_afc_switch_product.name}. 👍\n\n{_afc_switch_sq}" if _afc_switch_sq else (
                f"Sure, switching to {_afc_switch_product.name}. 👍"
            )
            try:
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", ai_reply)
            except Exception:
                pass
            try:
                await record_usage(db, client, conv)
            except Exception:
                pass
            out.early_result = PipelineResult(text=ai_reply)
            return out
        elif _afc_switch_product is None:
            # No catalog match either — don't re-echo the summary; give an
            # explicit escape hatch instead of looping.
            _afc_no_match_reply = (
                f"Couldn't find '{user_text.strip()}' — reply 1️⃣/2️⃣/3️⃣, "
                "or send the product name/code you want instead."
            )
            try:
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", _afc_no_match_reply)
            except Exception:
                pass
            try:
                await record_usage(db, client, conv)
            except Exception:
                pass
            out.early_result = PipelineResult(text=_afc_no_match_reply)
            return out
        # else: matched product IS the currently-pinned one (customer just
        # repeated the same code/name) — fall through to the normal
        # show_summary render below, same as any other unrecognized reply.

    # order_collection / awaiting_final_confirmation: resolve action from the
    # transition table. (This function is only called for AFC, but the
    # computation is preserved verbatim rather than hardcoded — see docstring.)
    _eff_intent: str
    if classified_intent in ("CANCEL", "DISCOUNT_QUERY", "OFF_TOPIC",
                              "NEW_PRODUCT", "NEW_PRODUCT_OOS"):
        _eff_intent = classified_intent
    elif next_slot is None:
        # All slots filled → synthetic SLOTS_DONE triggers summary.
        _eff_intent = "SLOTS_DONE"
    else:
        # ANSWER / OTHER / AUTO_SWITCH_DONE / switch-resolved / etc.
        _eff_intent = "ANSWER"
    _, _render_action = order_state_machine.resolve_transition(stage, _eff_intent)

    # Extract OOS product name from intent_override when needed.
    _oos_nm = (
        intent_override[4:]
        if (intent_override and intent_override.startswith("OOS:"))
        else None
    )

    # ── Single render call → customer text ────────────────────────
    try:
        ai_reply = await _render_order_reply(
            action=_render_action,
            conv=conv,
            db=db,
            client=client,
            next_slot=next_slot,
            variant_info=variant_info,
            customer_profile=customer_profile,
            available_stock=available_stock,
            declined_saved_address=declined_saved_address,
            lang=lang,
            is_first_slot=is_first_slot,
            oos_product_name=_oos_nm,
        )
    except RenderError as _re:
        logger.error(
            "RenderError conv=%s stage=%r action=%r — refusing to send: %s",
            conv.id, stage, _render_action, _re,
        )
        out.render_error = True
        return out

    # ── Mark summary_shown after show_summary render ──────────────
    if _render_action == "show_summary" and not getattr(conv, "summary_shown", False):
        try:
            await conversation_service.update_order_field(db, conv.id, "summary_shown", True)
            conv.summary_shown = True
        except Exception as exc:
            logger.error("summary_shown update error: %s", exc)

    out.text = ai_reply
    out.render_action = _render_action
    return out


# ---------------------------------------------------------------------------
# SLICE 6 — LLM call routing / tiered cascade for text messages in browsing
# stages (deterministic-template tiers, then catalog-match templates, then
# the soft-cap template, then the 70B classify_turn() call as last resort)
# ---------------------------------------------------------------------------

@dataclass
class RoutingOutcome:
    """
    Result of run_llm_routing().

    Only ever called for text messages in browsing stages (greeting,
    product_inquiry, qualification, objection_handling, offer_making) — the
    order-stage branches (slices 4-5) and the audio/image message-type
    branches stay inline in webhook.py, out of scope for this slice.

    text:       The customer-facing reply (already phantom-guarded when a
                canonical_browse_products set was available — the same
                catalogue_service.guard_product_reply() call that ran
                unconditionally at the end of the original inline block).
    llm_called: True when the 70B classify_turn() call fired this turn —
                the caller must OR this into its own _llm_called_this_turn.
    llm_usage:  gemini_service.get_last_usage() result when llm_called is
                True, else None — the caller splices this into _llm_usage
                for cost logging, exactly as the original inline code did.
    """

    text: str = ""
    llm_called: bool = False
    llm_usage: dict | None = None


async def run_llm_routing(
    db,
    conv,
    client,
    user_text: str,
    stage: str,
    language: str,
    history_dicts: list,
    system_prompt: str,
    catalogue_context: str,
    _canonical_browse_products: list,
    pinned_product,
    variant_info: dict,
    _pick_just_resolved: bool,
    _llm_budget: str,
    _llm_calls_today: int,
    _name_match_count: int,
    _multi_match_this_turn: bool = False,
    catalogue_products: list | None = None,
) -> RoutingOutcome:
    """
    stage: the final stage value from SLICE 4's SlotOutcome — passed in
    explicitly, never re-derived. This function is only ever invoked by
    webhook.py when stage is one of the browsing stages (the caller already
    checked `stage in _order_stages` is False before calling).

    pinned_product / variant_info / catalogue_context / canonical_browse_products
    / pick_just_resolved: SLICE 3's run_sku_and_name_pinning() outputs,
    threaded straight through (never re-matched, never re-fetched) — this is
    the exact near-miss the slice-3 docstring warns about: the keyword/name
    match already ran once this turn; this function reads its result, it
    does not run catalogue_service.search_products_with_scores() again to
    decide the tier (the one call it does make, for FIX1's _pinned_relevant
    check, searches only the single already-pinned product to score
    relevance — not a fresh catalogue-wide match — exactly as the original
    inline code did).

    name_match_count: webhook's `_name_match_count` local, always 0 in the
    current code (nothing currently increments it) — threaded through as-is
    rather than hardcoded, so a future change to its assignment upstream
    keeps working without touching this function.

    catalogue_products: the client's full active catalogue, already fetched
    by the caller this turn. Passed to guard_product_reply()'s all_products
    so its "not found" fallback lists real catalogue products instead of
    whatever canonical_browse_products happens to hold (often just a single
    stale pinned product from an earlier turn).

    Mirrors webhook.py's original inline tiered-cascade block verbatim (the
    delivery-query / FIX1 pinned-known-fact / catalog-multi-template / soft-
    cap / classify_turn+render_reply tiers, the ROUTE logging, and the final
    unconditional phantom-guard pass) — moved here unchanged as part of the
    strangler-fig extraction. `conv` is mutated in place via direct
    attribute sets (last_shown_sku), exactly as in the original inline code.
    """
    ai_reply = ""
    _llm_called_this_turn = False
    _llm_usage = None
    _BROWSING_STAGES_GATE = frozenset({
        "greeting", "product_inquiry", "qualification",
        "objection_handling", "offer_making",
    })

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
    elif _pinned_for_reply and stage in _BROWSING_STAGES_GATE and not _multi_match_this_turn and (
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
        # not _multi_match_this_turn: _pinned_relevant/_is_generic_avail score only
        # against the single already-pinned product, so a shared generic word (e.g.
        # "saree") or an "available?"-style question scores as relevant even when the
        # customer named a DIFFERENT product this turn. When the name-match pinner just
        # found 2+ real catalogue candidates for this message, that fresh, full-catalogue
        # signal must win over the stale single-product one — otherwise this template
        # answers with whatever was pinned before, ignoring what was actually asked.
        _pn2 = _pinned_for_reply.name or conv.pending_product_sku
        _psku2 = getattr(_pinned_for_reply, "sku", None) or conv.pending_product_sku
        _av_colors2 = variant_info.get("available_colors", [])
        _av_sizes2 = variant_info.get("available_sizes", [])
        _lines2 = []
        if _pick_just_resolved:
            # This is the first reveal of the product this turn (resolved from a
            # "do you sell X?" / multi-choice question, not a price/available? follow-up
            # about a product already shown) — lead with a plain acknowledgment so the
            # product card doesn't appear with no context, per user feedback.
            _lines2.append("Yes, we have this available:")
            _lines2.append("")
        _lines2.append(
            f"{_pn2} [{_psku2}] — {format_price(getattr(_pinned_for_reply, 'price', 0) or 0)}"
        )
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
            from app.config import get_settings as _gs6
            _settings = _gs6()
            _soft_url = f"{_settings.catalogue_base_url}/{_soft_slug}" if _soft_slug else _settings.catalogue_base_url
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
                    ai_reply = _render_reply.render_product_list_reply(_listed_products, language=language)
                else:
                    _biz_name = (getattr(client, "business_name", None) or "our store") if client else "our store"
                    ai_reply = _render_reply.render_open_browsing_reply(
                        _intent_result, pinned_product, variant_info, client, _biz_name, user_text,
                        language=language,
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
                    _intent_result, _resolved_product, _resolved_variant_info, client, _biz_name, user_text,
                    language=language,
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
    # BUG 1 defense-in-depth: when a multi-choice pick just resolved this
    # turn, pass its product as pre_validated_products so the guard never
    # flags that SKU/price as phantom, even if _canonical_browse_products
    # (rebuilt above) somehow doesn't carry it — the pick is already
    # confirmed valid, not an LLM claim to re-verify.
    _pre_validated = [pinned_product] if (_pick_just_resolved and pinned_product) else None
    if _canonical_browse_products or _pre_validated:
        ai_reply = catalogue_service.guard_product_reply(
            ai_reply, _canonical_browse_products, query=user_text,
            pre_validated_products=_pre_validated,
            all_products=catalogue_products,
        )

    return RoutingOutcome(text=ai_reply, llm_called=_llm_called_this_turn, llm_usage=_llm_usage)


# ---------------------------------------------------------------------------
# SLICE 7 — order creation + payment / money lifecycle
# (relocated _reset_order_slots_after_completion, plus the new
# run_order_payment() orchestration)
# ---------------------------------------------------------------------------

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
        # Phase 1 cart engine (migration 0057) — a paid/completed order must
        # never leak its cart-building state into the next order cycle.
        ("cart_items", None),
        ("cart_variant_mode", None),
        ("cart_collection_mode", None),
        ("cart_wip_item", None),
        ("cart_pending_confirmation", None),
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


@dataclass
class OrderOutcome:
    """
    Result of run_order_payment().

    This function never sends anything — it only creates/updates Order rows,
    deducts stock (via order_service.mark_order_paid(), which deducts
    internally), resets completion-cycle slots, and updates `stage` when the
    hard-completeness guard reverts an incomplete order back to
    order_collection. It does NOT alter the customer-facing reply text — by
    the time this runs, slice 5/6 (or the still-inline payment/completed
    rendering in webhook.py) has already produced `ai_reply` for this turn.

    SLICE 7 ↔ SLICE 8 HAND-OFF: the only field slice 8 (button/list dispatch)
    actually reads today is `stage` — webhook.py splices it back into its
    local `stage` before deciding button_type, exactly as the original inline
    code's mid-block `stage = "order_collection"` revert flowed straight into
    the button-type decision a few lines later. `order_created` / `order` /
    `payment_confirmed` are exposed for completeness and for any future
    slice-8 logic that wants to know whether an order/payment event happened
    this turn, but nothing downstream consumes them yet.

    One exception requires a small hand-off of its own: the original inline
    code called `_send_bank_transfer_details()` (a whatsapp_service-calling
    helper) directly when `resolved_payment == "bank_transfer"`. Since this
    module never calls whatsapp_service, that send is now the caller's
    responsibility — webhook.py must check
    `out.order_created and out.order.payment_method == "bank_transfer"`
    after calling this function and perform that one send itself (same
    try/except + warning log as before).

    stage:             Final stage value after this turn's order/payment
                        processing — may differ from the `stage` passed in
                        when the hard-completeness guard reverted it to
                        "order_collection".
    order_created:      True when a new Order row was created this turn.
    payment_confirmed:  True when an existing pending_payment order was
                        marked paid this turn (the UPI "paid" flow).
    order:              The Order row created or paid this turn, if any.
    """

    stage: str = "greeting"
    order_created: bool = False
    payment_confirmed: bool = False
    order: object = None
    #: Set when order creation was attempted this turn and FAILED. The caller
    #: must then suppress any payment/confirmation text already rendered for
    #: this turn — otherwise the customer is asked to pay for an order that
    #: does not exist (observed live: qty 40 > stock 10 blocked the order, but
    #: "Please pay ₹159,960" was still delivered).
    #: Shape: {"kind": str, "requested_qty": int | None, "stock": int | None}
    order_error: dict | None = None

async def run_order_payment(
    db,
    conv,
    client,
    stage: str,
    _stored_stage: str,
    sender_phone: str,
    user_text: str,
    wamid: str | None,
    history_dicts: list,
    available_stock: int | None,
    variant_info: dict,
    find_sku_matched_products,
) -> OrderOutcome:
    """
    stage / _stored_stage: the final stage from SLICE 4-6 and the turn's
    pre-mutation stored stage — both passed in explicitly, never re-derived
    (no fresh detect_stage(), no live conv.current_stage read to decide
    order-readiness). Mirrors stored_stage/stage threading in SLICES 3-6.

    available_stock: SLICE 4's SlotOutcome.available_stock for the PINNED
    product — threaded through unchanged. NOTE: the original inline code's
    qty-exceeds-stock warning log references this outer `available_stock`
    rather than the variant-specific `_oc_stock` it just computed two lines
    above — that mismatch is preserved verbatim (no bug fixes per the
    strangler-fig rules), so this parameter exists to keep that exact log
    line working, not because it is the value actually used for the
    blocking decision.

    variant_info: SLICE 3/4's variant_info for the pinned product — used
    only for the hard-completeness guard's needs_color/needs_size/
    needs_material checks, never re-fetched.

    find_sku_matched_products: webhook module's `_find_sku_matched_products`
    callable, passed in rather than imported, same pattern as SLICE 3.

    Mirrors webhook.py's original inline order-creation/payment block
    verbatim (DB writes, logging, the three early-return-via-exception
    control-flow paths inside the auto-create try block) — moved here
    unchanged as part of the strangler-fig extraction. `conv` is mutated in
    place via direct attribute sets, exactly as in the original inline code.
    """
    out = OrderOutcome(stage=stage)

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
                out.payment_confirmed = True
                out.order = _pay_order
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
                    sku_products = await find_sku_matched_products(db, client, user_text)
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

                # ── Phantom-product hard block ──────────────────────────────
                # None of the three resolution tiers above found a real
                # catalogue row. Never create an order for a product that
                # doesn't resolve to a real SKU — this is the last checkpoint
                # before a DB Order row is written, so it must be a hard stop,
                # not a fallback to "Unknown"/₹0. Reset the corrupted product/
                # variant/qty slots (keep name+address — not product-specific)
                # and let run_llm_routing's order_error handling replace
                # whatever summary/payment text was already rendered this turn
                # with an honest "couldn't find that product" reply.
                if not product:
                    _phantom_requested_qty = getattr(conv, "pending_order_quantity", None)
                    logger.error(
                        "Order creation BLOCKED — pinned_sku=%r did not resolve to a "
                        "real catalogue product for conv=%s. Resetting product/order slots.",
                        pinned_sku, conv.id,
                    )
                    _phantom_reset_fields = [
                        ("pending_product_sku", None), ("interrupted_sku", None),
                        ("last_shown_sku", None), ("pending_choice_skus", None),
                        ("selected_color", None), ("selected_size", None),
                        ("selected_material", None), ("pending_order_quantity", None),
                        ("summary_shown", False), ("cart_items", None),
                        ("cart_variant_mode", None), ("cart_collection_mode", None),
                        ("cart_wip_item", None), ("cart_pending_confirmation", None),
                    ]
                    for _pf, _pv in _phantom_reset_fields:
                        try:
                            await conversation_service.update_order_field(db, conv.id, _pf, _pv)
                            setattr(conv, _pf, _pv)
                        except Exception as exc:
                            logger.error("Phantom-product slot reset error (%s): %s", _pf, exc)
                    stage = "product_inquiry"
                    try:
                        await conversation_service.update_stage(db, conv.id, "product_inquiry")
                        conv.current_stage = "product_inquiry"
                    except Exception as exc:
                        logger.error("Stage revert after phantom-product block: %s", exc)
                    out.order_error = {
                        "kind": "product_not_found",
                        "requested_qty": _phantom_requested_qty,
                        "stock": None,
                        "product_name": None,
                    }
                    raise ValueError("order_blocked:product_not_found")

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
                        # Capture the requested qty BEFORE clearing it — the
                        # reset below used to run first, so this log and the
                        # raised message both reported "None" as the quantity.
                        _requested_qty = conv.pending_order_quantity
                        logger.warning(
                            "Order blocked: requested qty %d > stock %d for product %s",
                            _requested_qty, _oc_stock, product.sku,
                        )
                        # Reset quantity so it can be re-collected correctly next turn
                        try:
                            await conversation_service.update_order_field(
                                db, conv.id, "pending_order_quantity", None
                            )
                            conv.pending_order_quantity = None
                        except Exception:
                            pass
                        # Send the customer back to slot collection: staying in
                        # `payment` left the flow asking for money for an order
                        # that was never created.
                        stage = "order_collection"
                        try:
                            await conversation_service.update_stage(
                                db, conv.id, "order_collection"
                            )
                            conv.current_stage = "order_collection"
                        except Exception as _se:
                            logger.error("Stage revert after qty-exceeds-stock: %s", _se)
                        out.order_error = {
                            "kind": "qty_exceeds_stock",
                            "requested_qty": _requested_qty,
                            "stock": _oc_stock,
                            "product_name": getattr(product, "name", None),
                        }
                        raise ValueError(
                            f"qty_exceeds_stock:{_requested_qty}:{_oc_stock}"
                        )

                # ── Hard completeness guard ───────────────────────────────
                # All required fields must be present before any DB write.
                # Variant fields are required when the product declares them.
                _missing_fields: list[str] = []
                if not conv.customer_name:
                    _missing_fields.append("customer_name")
                if not conv.delivery_address:
                    _missing_fields.append("delivery_address")
                if not getattr(conv, "mobile_number", None):
                    _missing_fields.append("mobile_number")
                if not conv.pending_order_quantity:
                    _missing_fields.append("pending_order_quantity")
                if not conv.payment_method:
                    _missing_fields.append("payment_method")
                _vi_check = variant_info or {}
                # Phase 1 cart engine — a "different for each" cart (whether
                # reached via the normal variant_mode question or via a
                # multi-color single-message seed, e.g. "10 red and 10 pink")
                # never writes selected_color/selected_size/selected_material
                # at all — each line item carries its own color/size/material
                # in cart_items instead. Only check the flat columns for the
                # simple/"same" path, where they're the actual source of truth.
                if getattr(conv, "cart_variant_mode", None) != "different":
                    if _vi_check.get("needs_color") and not getattr(conv, "selected_color", None):
                        _missing_fields.append("selected_color")
                    if _vi_check.get("needs_size") and not getattr(conv, "selected_size", None):
                        _missing_fields.append("selected_size")
                    if _vi_check.get("needs_material") and not getattr(conv, "selected_material", None):
                        _missing_fields.append("selected_material")
                # Phase 1 cart engine — cart_items is non-empty for BOTH the
                # "different for each" path (item 1 is folded in the instant
                # its own qty is known — see _apply_cart_extraction) AND a
                # completed "Add {product} too" cross-sell (folded above when
                # the second product's own flat-field selection completes) —
                # either way, cart_items is the authoritative source of what
                # to actually create.
                _is_cart_order = bool(getattr(conv, "cart_items", None))
                if getattr(conv, "cart_variant_mode", None) == "different":
                    # Belt-and-braces check, only meaningful for the "different
                    # for each" flow where pending_order_quantity is the single
                    # target N. By construction this can never actually fail
                    # here — get_next_required_slot only returns None (letting
                    # summary/confirm/payment be reached at all) once
                    # sum(cart_items qty) == pending_order_quantity — kept
                    # anyway so a corrupted/hand-edited row can never place an
                    # order for pieces that were never actually collected.
                    # (A cross-sell-accumulated cart has no single N to check
                    # against — pending_order_quantity there is just the last
                    # product's own qty — so this check is skipped for it.)
                    _cart_chk = getattr(conv, "cart_items", None) or []
                    if not _cart_chk or sum(i["qty"] for i in _cart_chk) != (conv.pending_order_quantity or 0):
                        _missing_fields.append("cart_items")

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
                if _is_cart_order:
                    # Phase 1 cart engine, "different for each" path — one
                    # Order header + N OrderLineItem rows, one order_number.
                    _cart_line_items = [
                        {
                            "product_id": product.id if product else None,
                            "product_name": i.get("product_name") or (product.name if product else "Unknown"),
                            "product_sku": i.get("sku") or (product.sku if product else None),
                            "variant_color": i.get("color"),
                            "variant_size": i.get("size"),
                            "variant_material": i.get("material"),
                            "quantity": i["qty"],
                            "unit_price": i.get("unit_price") or (product.price if product else 0.0),
                        }
                        for i in (getattr(conv, "cart_items", None) or [])
                    ]
                    created_order = await order_service.create_cart_order(
                        db=db,
                        client_id=client.id,
                        conversation_id=conv.id,
                        customer_name=conv.customer_name,
                        customer_phone=sender_phone,
                        delivery_address=conv.delivery_address,
                        mobile_number=getattr(conv, "mobile_number", None),
                        line_items=_cart_line_items,
                        payment_method=resolved_payment,
                        idempotency_key=wamid,
                    )
                else:
                    # Simple single-item path (no variants, qty=1, or
                    # variant_mode="same") — unchanged call, unchanged behavior.
                    created_order = await order_service.create_order(
                        db=db,
                        client_id=client.id,
                        conversation_id=conv.id,
                        customer_name=conv.customer_name,
                        customer_phone=sender_phone,
                        delivery_address=conv.delivery_address,
                        mobile_number=getattr(conv, "mobile_number", None),
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
                out.order_created = True
                out.order = created_order
                _order_initial_status = created_order.status
                logger.info(
                    "Auto-created order %s (status=%s) from conversation %s",
                    created_order.order_number, _order_initial_status, conv.id,
                )
                cost_log.print_report(conv.id, created_order.order_number)

                # Bank transfer: details are sent as a separate message by the
                # caller (webhook.py) AFTER this function returns — building/
                # sending that message requires whatsapp_service, which this
                # channel-neutral module never calls directly. Signaling via
                # out.order (the caller checks order.payment_method) is the
                # slice-7↔8-style hand-off: see OrderOutcome docstring.

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
                if out.order_error is None:
                    # Any unclassified failure still means "no order exists" —
                    # the caller must not deliver payment instructions for it.
                    out.order_error = {
                        "kind": "order_not_created",
                        "requested_qty": getattr(conv, "pending_order_quantity", None),
                        "stock": None,
                        "product_name": None,
                    }

    out.stage = stage
    return out


# ---------------------------------------------------------------------------
# SLICE 8 — button/list/text dispatch decision (channel-neutral; the
# WhatsApp-specific nonce encoding + actual send lives in
# app/routers/_whatsapp_adapter.py)
# ---------------------------------------------------------------------------

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



async def decide_send_instruction(
    db,
    conv,
    client,
    stage: str,
    ai_reply: str,
    next_slot: str | None,
    is_whatsapp: bool,
    pinned_product,
) -> PipelineResult:
    """
    Decide HOW this turn's reply should be delivered: plain text, payment
    buttons, confirm/cancel buttons, a paid-confirmation button, an offer
    yes/no, a 2-3 option button choice, or a 4+ option list — returned as a
    channel-neutral PipelineResult ("SendInstruction") with RAW (un-encoded)
    button/row ids. The WhatsApp adapter encodes ids with a nonce and
    performs the actual send; this function never touches whatsapp_service
    or nonce state.

    stage / next_slot: threaded explicitly from SLICE 4-7 (SlotOutcome /
    OrderOutcome.stage) — never recomputed. pinned_product: threaded from
    SLICE 3/4 — never re-matched. conv.pending_product_sku /
    conv.pending_choice_skus are read directly because they are this turn's
    already-finalized slot state (the same pattern every prior slice uses
    for conv attributes), not a value decided by a separate upstream
    computation that would need explicit threading.

    Mirrors webhook.py's original inline button_type decision (the
    _should_use_buttons call, the offer/choice upgrade, the completed/payment
    overrides, and the payment_buttons button-list construction) verbatim —
    moved here unchanged as part of the strangler-fig extraction.
    """
    accepts_cod = getattr(client, "accepts_cod", False) if client else False
    button_type = _should_use_buttons(stage, ai_reply, accepts_cod, next_slot=next_slot) if is_whatsapp else "text"

    offer_buttons: list[dict] = []
    choice_buttons: list[dict] = []
    choice_list_rows: list[dict] = []

    # ── E2: interactive buttons/list for product offers and multi-option choices ──
    # Purely additive on top of the text reply already built above — typing
    # "yes"/"1"/a product name still resolves exactly as before; tapping is
    # just a faster path to the same outcome.
    if is_whatsapp and button_type == "text":
        if (
            "would you like to order?" in ai_reply.lower()
            and pinned_product is not None
            and getattr(conv, "pending_product_sku", None)
        ):
            button_type = "offer_buttons"
            offer_buttons = [
                {"id": "offer_yes", "title": "Yes"},
                {"id": "offer_no", "title": "No"},
            ]
        else:
            pcs_for_buttons: list = []
            try:
                pcs_raw_btn = getattr(conv, "pending_choice_skus", None)
                if pcs_raw_btn:
                    pcs_for_buttons = _json.loads(pcs_raw_btn)
            except Exception:
                pcs_for_buttons = []
            if pcs_for_buttons and client:
                choice_products = []
                for cb_sku in pcs_for_buttons:
                    try:
                        cb_p = await catalogue_service.find_product_by_sku(db, client.id, cb_sku)
                    except Exception:
                        cb_p = None
                    if cb_p:
                        choice_products.append(cb_p)
                if 2 <= len(choice_products) <= 3:
                    # WhatsApp allows max 3 buttons — one per product, id=SKU.
                    button_type = "choice_buttons"
                    for cb_p in choice_products:
                        choice_buttons.append({
                            "id": cb_p.sku,
                            "title": (cb_p.name or cb_p.sku)[:20],
                        })
                elif len(choice_products) >= 4:
                    # 4+ options: WhatsApp buttons cap at 3 — use a list message instead.
                    button_type = "choice_list"
                    for cb_p in choice_products[:10]:
                        choice_list_rows.append({
                            "id": cb_p.sku,
                            "title": (cb_p.name or cb_p.sku)[:24],
                            "description": format_price(getattr(cb_p, "price", 0) or 0),
                        })

    # Hard safety net: the "Order confirmed!" message must NEVER carry interactive
    # buttons regardless of what happened inside the order creation block.
    # If the guard or an exception inside that block reverted stage to something
    # other than "completed" but ai_reply still contains "confirm", _should_use_buttons
    # might return confirm_buttons.  We override that here for the completed case.
    if stage == "completed":
        button_type = "text"
    # Payment stage: show "I've Paid" button alongside the UPI instructions.
    # "paid_done" maps to "paid" in the interactive parser (already wired).
    elif stage == "payment" and is_whatsapp:
        button_type = "paid_button"

    if button_type == "payment_buttons":
        # Build button list based on enabled payment methods for this client/order
        order_total_for_buttons = 0.0
        if conv and pinned_product:
            qty = getattr(conv, "pending_order_quantity", None) or 0
            order_total_for_buttons = qty * (getattr(pinned_product, "price", 0) or 0)

        accepts_upi = getattr(client, "accepts_upi", True) if client else True
        upi_id_set = bool(getattr(client, "upi_id", None)) if client else False
        cod_limit = getattr(client, "cod_limit", None) if client else None
        accepts_bank = getattr(client, "accepts_bank_transfer", False) if client else False
        bank_ready = bool(
            getattr(client, "bank_account_name", None)
            and getattr(client, "bank_account_number", None)
            and getattr(client, "bank_ifsc", None)
        ) if client else False

        cod_eligible = (
            accepts_cod
            and (cod_limit is None or order_total_for_buttons <= cod_limit)
        )

        payment_buttons = []
        if accepts_upi and upi_id_set:
            payment_buttons.append({"id": "upi", "title": "💳 Pay via UPI"})
        if cod_eligible:
            payment_buttons.append({"id": "cod", "title": "🚚 Cash on Delivery"})
        if accepts_bank and bank_ready:
            payment_buttons.append({"id": "bank_transfer", "title": "🏦 Bank Transfer"})

        # WhatsApp buttons support max 3; if none configured fall back to text
        payment_buttons = payment_buttons[:3]

        if len(payment_buttons) <= 1:
            # Only one option — no need for a button choice; send AI reply as text
            return PipelineResult(text=ai_reply)
        return PipelineResult(
            text=ai_reply,
            buttons=[ButtonSpec(id=b["id"], title=b["title"]) for b in payment_buttons],
        )

    if button_type == "confirm_buttons":
        return PipelineResult(
            text=ai_reply,
            buttons=[
                ButtonSpec(id="confirm_pay", title="Confirm & Pay"),
                ButtonSpec(id="cancel_order", title="Cancel"),
            ],
        )

    if button_type == "paid_button":
        return PipelineResult(
            text=ai_reply,
            buttons=[ButtonSpec(id="paid_done", title="I've Paid")],
        )

    if button_type == "offer_buttons":
        return PipelineResult(
            text=ai_reply,
            buttons=[ButtonSpec(id=b["id"], title=b["title"]) for b in offer_buttons],
        )

    if button_type == "choice_buttons":
        return PipelineResult(
            text=ai_reply,
            buttons=[ButtonSpec(id=b["id"], title=b["title"]) for b in choice_buttons],
        )

    if button_type == "choice_list":
        return PipelineResult(
            text=ai_reply,
            list_options=[
                ListOptionSpec(id=r["id"], title=r["title"], description=r["description"])
                for r in choice_list_rows
            ],
            list_header="Choose an option",
            list_button_text="View options",
        )

    return PipelineResult(text=ai_reply)


# ---------------------------------------------------------------------------
# Channel-neutral request-scoped helpers (moved verbatim from webhook.py as
# part of the handle_inbound_message consolidation — none of these call any
# channel send API).
# ---------------------------------------------------------------------------


def _get_system_prompt(client) -> str | None:
    """Return the Gemini system prompt for the resolved client, or None."""
    return client.gemini_system_prompt if client else None


async def _record_usage(db, client, conv=None) -> None:
    """
    Record one message in UsageLog and one conversation-activity tick toward
    the client's plan conv_limit (no-op if client is None; conv is optional
    since not every call site has a resolved Conversation).
    """
    if client:
        await usage_service.record_message(db, client)
        if conv is not None:
            await billing_service.record_conversation_activity(db, client, conv)


async def _get_catalogue_context(db, client, user_text: str, conv=None) -> "tuple[str | None, list]":
    """
    Build catalogue context for Gemini from a customer message.

    Strategy (priority order):
      1. If conv.pending_product_sku is set, load that product first — ensures the
         agent always discusses the same product the customer originally asked about,
         even when the customer's reply is a short/ambiguous follow-up.
      2. Scan user_text for SKU-like tokens (e.g. SR27754). If found, look up those
         products by exact SKU.
      3. Keyword search across the full catalogue, top 5 most relevant products.

    Returns a tuple of (formatted catalogue string or None, list of Product instances).
    """
    if client is None:
        return None, []

    pinned_sku = getattr(conv, "pending_product_sku", None) if conv else None
    if pinned_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
        if pinned:
            return catalogue_service.format_catalogue_context([pinned], for_display=True), [pinned]

    skus = catalogue_service.extract_skus_from_text(user_text)
    if skus:
        sku_products = []
        for sku in skus:
            p = await catalogue_service.find_product_by_sku(db, client.id, sku)
            if p:
                sku_products.append(p)
        if sku_products:
            return catalogue_service.format_catalogue_context(sku_products, for_display=True), sku_products

    products = await catalogue_service.list_products(db, client.id)
    relevant = catalogue_service.search_products(products, user_text)
    if not relevant:
        return None, []
    return catalogue_service.format_catalogue_context(relevant, for_display=True), relevant


async def _find_sku_matched_products(db, client, user_text: str) -> list:
    """
    Return the catalogue products whose SKU is explicitly quoted in the message.

    Standalone helper (separate from _get_catalogue_context) so the orchestrator
    can send a product photo for an exact SKU match — Learning 4: "when a
    customer quotes a SKU, send the product image first, then the text
    details" — without re-running the SKU scan or changing
    _get_catalogue_context's contract.
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


async def _is_product_out_of_stock(db, product) -> bool:
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


def _build_bank_transfer_text(amount_inr: float, order_number: str, client) -> str | None:
    """
    Build the bank-transfer payment-details message text, or None when the
    client's bank details are incomplete (mirrors webhook.py's original
    _send_bank_transfer_details — minus the actual send, which the caller's
    channel adapter now performs via PipelineResult.pre_texts).
    """
    _account_name = getattr(client, "bank_account_name", None) if client else None
    _account_number = getattr(client, "bank_account_number", None) if client else None
    _ifsc = getattr(client, "bank_ifsc", None) if client else None
    _payment_instructions = getattr(client, "payment_instructions", None) if client else None

    if not (_account_name and _account_number and _ifsc):
        logger.warning("Bank transfer details incomplete for client %s", getattr(client, "id", "?"))
        return None

    instructions_part = f"\n\n{_payment_instructions}" if _payment_instructions else ""
    return (
        f"Please transfer {format_price(amount_inr)} to complete your order {order_number}.\n"
        f"Account Name: {_account_name}\n"
        f"Account Number: {_account_number}\n"
        f"IFSC: {_ifsc}{instructions_part}\n\n"
        f"Reply PAID when done. ✅"
    )


@dataclass
class InboundContext:
    """
    Channel-neutral input bundle for handle_inbound_message().

    Built by a channel router (e.g. app/routers/webhook.py for WhatsApp) from
    its own channel-specific payload parsing, then handed to the orchestrator.
    A future Instagram handler would build its own InboundContext (using its
    own message-shape parsing and vision_service.download_instagram_media as
    download_media) and call handle_inbound_message() with no other changes.

    db / client / conv:    Already-resolved async DB session, Client, and
                            Conversation rows — resolution itself is
                            channel-specific (e.g. WhatsApp resolves by
                            phone_number_id) and stays in the caller.
    sender_phone:           Customer identifier on this channel.
    message:                The channel's already-parsed inbound message
                            object — still consumed via .type/.text/.image/
                            .audio/.interactive by the slice functions exactly
                            as webhook.py did inline.
    user_text:              Normalized text for this turn (button/list taps
                            already mapped to canonical text by the caller).
    wamid:                  Channel message id, if any (used for dedup/save).
    btn_nonce_parsed:       Decoded (action, conv_id, nonce) tuple from an
                            interactive button/list id, if the channel uses
                            nonce-encoded ids (WhatsApp does; IG would pass
                            None — IG's tap payload already carries enough
                            context with no nonce concept needed).
    download_media:         Async callable(media_id_or_url) -> bytes, the
                            channel-specific media fetch (WhatsApp: two-step
                            media_id → CDN URL → bytes via Graph API; IG:
                            direct CDN URL fetch) — injected so the
                            orchestrator can stay channel-neutral while still
                            honoring the original order_flow short-circuit
                            that skips the download entirely when an image/
                            audio message can't advance the slot machine.
    is_whatsapp:            Whether this channel supports WhatsApp-style
                            interactive messages (buttons/lists) — threaded
                            into decide_send_instruction exactly as
                            webhook.py's inline `is_whatsapp` was.
    """

    db: object
    client: object
    conv: object
    sender_phone: str
    message: object
    user_text: str
    wamid: "str | None"
    btn_nonce_parsed: "tuple[str, str, str] | None"
    download_media: object
    is_whatsapp: bool = True


async def handle_inbound_message(ctx: InboundContext) -> PipelineResult:
    """
    Single channel-neutral entry point for processing one inbound message.

    Consolidates the 8-slice strangler-fig sequence (early guards → SKU/name
    pinning → slot state machine → deterministic short-circuits → escalation
    → summary/routing → order/payment → dispatch decision) that previously
    ran inline inside webhook.py's receive_message(), under bare local-
    variable splicing between slices. This function now OWNS that splice-back
    state internally (pinned_product, variant_info, stage, next_slot, etc.)
    instead of leaving it to the caller.

    Never calls any channel send API directly — always returns a
    PipelineResult (the dispatch decision, or an early-exit reply/silence)
    for the caller's channel adapter to actually deliver. The only sends a
    caller must still perform itself, OUTSIDE this function, are channel-
    specific best-effort UX details that cannot be expressed as a single
    end-of-turn PipelineResult — currently just the WhatsApp audio
    acknowledgement (sent immediately, before transcription, by the caller
    during payload parsing).

    Channel-specific responsibilities the caller (e.g. webhook.py) keeps:
    payload parsing, client/conversation resolution, building InboundContext,
    resolving the channel's pid/phone_number_id, and choosing + invoking its
    channel adapter (e.g. _whatsapp_adapter.send_pipeline_result) on the
    returned PipelineResult.
    """
    db = ctx.db
    client = ctx.client
    conv = ctx.conv
    message = ctx.message
    sender_phone = ctx.sender_phone
    user_text = ctx.user_text
    wamid = ctx.wamid
    _btn_nonce_parsed = ctx.btn_nonce_parsed

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
        return await run_hard_llm_cap_guard(
            db, conv, client, sender_phone, user_text, wamid, _llm_calls_today,
        )

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
        return _stale_paid_result

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
        return _nonce_guard_result

    # ── Cancel-in-payment guard ───────────────────────────────────────────────
    # When customer is in the "payment" stage (UPI: order created as
    # pending_payment, waiting for paid confirmation) — or in the
    # "awaiting_switch_confirm" micro-stage sitting on top of it — and taps
    # the Cancel button or sends cancel-intent free text ("I don't want this",
    # "never mind"), neither the AFC CANCEL block (needs
    # stored=awaiting_final_confirmation) nor the CANCEL intent block (needs
    # stage=order_collection) fires. Handle it here as an early return: cancel
    # the pending_payment order and go to greeting. Runs BEFORE
    # run_switch_confirm_guard below so a cancel-intent reply to the
    # switch-confirm prompt cancels the whole order instead of being read as
    # an unclear yes/no on the switch itself.
    _cancel_in_payment_result = await run_cancel_in_payment_guard(
        db, conv, client, sender_phone, user_text, wamid, _stored_stage, _record_usage,
    )
    if _cancel_in_payment_result is not None:
        return _cancel_in_payment_result

    # Upsert customer profile — best-effort; never block the message flow.
    # Block listed customers get no AI reply.
    if await run_blocklist_guard(db, client, sender_phone):
        return PipelineResult(text=None, skip_send=True)

    # Human takeover: save message silently, skip AI entirely.
    _takeover_result = await run_human_takeover_guard(db, conv, sender_phone, user_text, wamid)
    if _takeover_result is not None:
        return _takeover_result

    # ── Duplicate "Confirm Order" tap guard ───────────────────────────────────
    # WhatsApp button messages remain tappable indefinitely after the chat moves on.
    # When the conversation is already in "completed" stage and the customer taps
    # "Confirm Order" (or any affirmative button that looks like a confirmation),
    # short-circuit with a friendly plain-text reply — no AI call, no escalation.
    _dup_confirm_result = await run_duplicate_confirm_tap_guard(
        db, conv, message.type, sender_phone, user_text, wamid,
    )
    if _dup_confirm_result is not None:
        return _dup_confirm_result

    # FIX E: Payment-confirmation words sent AFTER order is already completed
    # (e.g. customer taps "PAID" again, or sends "done"/"transferred") must
    # never trigger a fresh AI "Order confirmed!" reply or a second Order row.
    # Return a deterministic already-confirmed message and stop processing.
    _dup_payment_result = await run_duplicate_payment_word_guard(
        db, conv, message.type, sender_phone, user_text, wamid,
    )
    if _dup_payment_result is not None:
        return _dup_payment_result

    # ── Flow-state / context expiry (migration 0052) ─────────────────────────
    # Runs before any flow dispatch below so a STALE in-progress flow is never
    # resumed. Gated on actual elapsed time (_flow_state_expired), NOT merely
    # on "a flow happens to be active" — a bare "Hi" sent seconds after an
    # active yes/no gate (e.g. the saved-address confirm prompt) is legacy,
    # deliberately-pinned behavior: an unclear reply to be re-prompted by that
    # gate's own logic, not a request to abandon the flow. Only once the gap
    # actually exceeds FLOW_STATE_TTL does a greeting get to short-circuit
    # into a full reset. See the "Flow-state / context expiry" section above
    # for is_greeting_only / has_reference_pronoun / _reset_flow_state / TTL
    # definitions.
    _expiry_now = datetime.now(timezone.utc)
    _pre_expiry_stage = _stored_stage
    _flow_state_at = getattr(conv, "flow_state_at", None)
    _flow_state_expired = (
        _flow_state_at is not None
        and (_expiry_now - _flow_state_at) > _flow_state_ttl(client)
    )
    if _flow_state_expired and _pre_expiry_stage != "greeting":
        if message.type == "text" and is_greeting_only(user_text):
            return await _send_fresh_greeting(
                db, conv, client, sender_phone, user_text, wamid, _record_usage,
            )
        logger.info(
            "Flow-state expired: conv=%s stage=%r age=%s > ttl=%s — resetting to greeting.",
            conv.id, _pre_expiry_stage, _expiry_now - _flow_state_at, _flow_state_ttl(client),
        )
        await _reset_flow_state(db, conv)
        _stored_stage = "greeting"

    # ── Mid-payment switch-confirmation resolution ───────────────────────────
    # Runs before SKU/name-pinning and before detect_stage so a "yes"/"no"
    # reply to the switch-confirm prompt is never mistaken for a fresh SKU
    # search — mirrors how the existing browsing-stage interrupted_sku
    # confirmation also runs early, before any other name-match/SKU-pin logic.
    _switch_confirm_result = await run_switch_confirm_guard(
        db, conv, client, sender_phone, user_text, wamid, _record_usage,
    )
    if _switch_confirm_result is not None:
        return _switch_confirm_result

    # ── Cross-product order-switch confirmation resolution ──────────────────
    # Runs immediately alongside the mid-payment guard above, for the same
    # reason: a "yes"/"no" reply to the FIX2/FIX4 "Would you like to order
    # <named product>?" prompt must resolve here, before SKU/name-pinning
    # (run_sku_and_name_pinning's own interrupted_sku handler only covers
    # browsing stages, not this order_collection micro-stage) or detect_stage
    # ever see it.
    _order_switch_confirm_result = await run_order_switch_confirm_guard(
        db, conv, client, sender_phone, user_text, wamid, _record_usage,
    )
    if _order_switch_confirm_result is not None:
        return _order_switch_confirm_result

    # Gated on _is_availability_question too, not has_reference_pronoun alone —
    # bare "it"/"that" appear in plenty of unrelated instructions ("change it
    # to <address>", "cancel that order") that must keep going through their
    # own existing handlers, not get hijacked into a product-status answer.
    if (
        message.type == "text"
        and not is_greeting_only(user_text)
        and has_reference_pronoun(user_text)
        and _is_availability_question(user_text)
    ):
        _last_context = getattr(conv, "last_context", None)
        _last_context_at = getattr(conv, "last_context_at", None)
        if (
            _last_context
            and _last_context_at is not None
            and (_expiry_now - _last_context_at) <= _context_ttl(client)
        ):
            _context_ref_result = await _resolve_last_context_reference(
                db, conv, client, _last_context, user_text, sender_phone, wamid, _record_usage,
            )
            if _context_ref_result is not None:
                return _context_ref_result
            # Product from the snapshot no longer resolves (e.g. deleted) —
            # fall through to normal flow dispatch below.

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
        return _pin_outcome.early_result
    pinned_product = _pin_outcome.pinned_product
    variant_info = _pin_outcome.variant_info
    catalogue_context = _pin_outcome.catalogue_context
    _canonical_browse_products = _pin_outcome.canonical_browse_products
    _pick_just_resolved = _pin_outcome.pick_just_resolved
    _p03_repinned = _pin_outcome.p03_repinned
    _multi_match_this_turn = _pin_outcome.multi_match_this_turn
    customer_profile = None

    # Single write point for last_context/last_context_at (migration 0052):
    # every branch inside run_sku_and_name_pinning that resolves a product
    # converges on _pin_outcome.pinned_product, so hooking here — rather than
    # at each individual "set pending_product_sku" call site inside that
    # function — captures every one of them without duplicating this logic.
    if pinned_product is not None:
        try:
            await conversation_service.set_last_context(db, conv.id, pinned_product)
            conv.last_context = {
                "product_id": pinned_product.id,
                "sku": getattr(pinned_product, "sku", None),
                "name": pinned_product.name,
                "price": pinned_product.price,
            }
            conv.last_context_at = datetime.now(timezone.utc)
        except Exception as exc:
            logger.error("last_context update error: %s", exc)

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
                conversation_id=conv.id,
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

    # ── SLICE 4: slot state machine (delegated to order_pipeline) ────────────
    _slot_outcome = await run_slot_state_machine(
        db, conv, client, message, user_text, sender_phone, wamid, language,
        history_dicts, stage, _stored_stage, pinned_product, variant_info,
        _record_usage,
    )
    if _slot_outcome.early_result is not None:
        return _slot_outcome.early_result
    stage = _slot_outcome.stage
    pinned_product = _slot_outcome.pinned_product
    variant_info = _slot_outcome.variant_info
    available_stock = _slot_outcome.available_stock
    _next_slot = _slot_outcome.next_slot
    _current_instruction = _slot_outcome.current_instruction
    _quantity_invalid = _slot_outcome.quantity_invalid
    _combo_oos = _slot_outcome.combo_oos
    _declined_saved_address = _slot_outcome.declined_saved_address
    _is_first_slot = _slot_outcome.is_first_slot
    _confirmation_gate_fired = _slot_outcome.confirmation_gate_fired
    _intent_override = _slot_outcome.intent_override
    _classified_intent = _slot_outcome.classified_intent
    _switch_resolved = _slot_outcome.switch_resolved
    _afc_crosssell_switched = _slot_outcome.afc_crosssell_switched
    _crosssell_sku_for_session = _slot_outcome.crosssell_sku_for_session

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
                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                await conversation_service.save_message(db, conv.id, "assistant", safe_reply)
                return PipelineResult(text=safe_reply)

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
                return PipelineResult(text=empathy_reply)
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
            await _record_usage(db, client, conv)
        except Exception as exc:
            logger.error("Usage tracking error (catalogue): %s", exc)
        return PipelineResult(text=_cat_reply)

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
            await _record_usage(db, client, conv)
        except Exception as exc:
            logger.error("Usage tracking error (greeting): %s", exc)
        return PipelineResult(text=_g_reply)

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
                    await _record_usage(db, client, conv)
                except Exception:
                    pass
                return PipelineResult(text=_boundary_idle)
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
                await _record_usage(db, client, conv)
            except Exception:
                pass
            return PipelineResult(text=_ot_idle_reply)

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
            await _record_usage(db, client, conv)
        except Exception as exc:
            logger.error("Usage tracking error (order status): %s", exc)
        _log_route(conv.id, "TEMPLATE", "order_status_short_circuit", extra=f"found={bool(_os_order)}")
        logger.info("Order status short-circuit: conv=%s found=%s", conv.id, bool(_os_order))
        return PipelineResult(text=_os_reply)

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
                image_bytes = await ctx.download_media(message.image.id)
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
                if ai_reply is None:
                    # Vision call failed (already logged in vision_service) — fall
                    # back to text-only handling for this message instead of
                    # crashing or stalling the order flow.
                    ai_reply = (
                        "Image abhi process nahi ho payi 🙏 Aap product ka naam ya "
                        "SKU type kar denge? Turant help karta/karti hoon."
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
            # NOTE: the "🎤 Voice note suna..." acknowledgement is sent immediately
            # by the channel adapter during payload parsing (webhook.py), BEFORE
            # this orchestrator runs — preserves the original immediate-ack UX
            # (the customer should not wait for transcription+pipeline to finish
            # before seeing it), which a single end-of-turn PipelineResult cannot
            # reproduce. This is therefore the one channel-specific send that is
            # deliberately NOT routed through PipelineResult.
            audio_bytes = await ctx.download_media(message.audio.id)
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
                        _pq_cp = await _find_confident_product_match(
                            db, client, user_text, getattr(conv, "pending_product_sku", None)
                        )
                        if _pq_cp is not None:
                            _pq_answer = f"{_pq_cp.name} — ₹{int(getattr(_pq_cp, 'price', 0) or 0):,}."

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
                                await _record_usage(db, client, conv)
                            except Exception:
                                pass
                            return PipelineResult(text=_pq_reply)

                    # New-purchase / product-switch intent during payment wait
                    # ("I want to buy X", "switch to X") — must NOT silently
                    # overwrite the pending order, and must NOT be swallowed by
                    # the generic reminder fallback below. Ask explicitly and
                    # stash the candidate in interrupted_sku (the same field
                    # the browsing-stage interrupt-switch flow uses) + a
                    # dedicated micro-stage, resolved by run_switch_confirm_guard
                    # on the customer's next turn.
                    if message.type == "text" and is_purchase_intent(user_text):
                        _switch_candidate = await _find_confident_product_match(
                            db, client, user_text, getattr(conv, "pending_product_sku", None)
                        )
                        if _switch_candidate is not None:
                            _sw_cur_name = getattr(pinned_product, "name", None) or "your current product"
                            _sw_qty = getattr(conv, "pending_order_quantity", 1) or 1
                            _sw_cur_price = getattr(pinned_product, "price", 0) or 0
                            _sw_cur_total = format_price(_sw_qty * _sw_cur_price)
                            _switch_prompt = (
                                f"You have a pending payment for {_sw_cur_name} ({_sw_cur_total}).\n"
                                f"Switch to {_switch_candidate.name} instead? "
                                "Reply 'yes' to switch or 'no' to keep current order."
                            )
                            try:
                                await conversation_service.update_order_field(
                                    db, conv.id, "interrupted_sku", _switch_candidate.sku
                                )
                                conv.interrupted_sku = _switch_candidate.sku
                                await conversation_service.update_stage(db, conv.id, "awaiting_switch_confirm")
                                conv.current_stage = "awaiting_switch_confirm"
                            except Exception as exc:
                                logger.error("Payment-stage switch-candidate stash error: %s", exc)
                            logger.info(
                                "Payment-stage purchase intent: conv=%s candidate=%s — awaiting switch confirm.",
                                conv.id, _switch_candidate.sku,
                            )
                            try:
                                await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
                                await conversation_service.save_message(db, conv.id, "assistant", _switch_prompt)
                            except Exception:
                                pass
                            try:
                                await _record_usage(db, client, conv)
                            except Exception:
                                pass
                            return PipelineResult(text=_switch_prompt)

                    # Payment stage always re-shows UPI instructions until PAID.
                    # (PAID detection is an early-return above; we only reach here
                    # when the customer sent something other than a payment word.)
                    _render_action = "reask_payment"
                elif stage == "awaiting_final_confirmation":
                    # ── SLICE 5: summary / final-confirmation rendering (delegated) ──
                    _summary_outcome = await run_summary_confirmation(
                        db, conv, client, message, user_text, wamid, stage, _tpl_lang,
                        _next_slot, variant_info, customer_profile, available_stock,
                        _declined_saved_address, _is_first_slot, _intent_override,
                        _classified_intent, pinned_product, _record_usage,
                    )
                    if _summary_outcome.early_result is not None:
                        return _summary_outcome.early_result
                    if _summary_outcome.render_error:
                        return PipelineResult(text=None, skip_send=True, status="render_error")
                    ai_reply = _summary_outcome.text
                    _render_action = _summary_outcome.render_action
                else:
                    # order_collection: resolve action from the transition table.
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

                if stage != "awaiting_final_confirmation":
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
                        return PipelineResult(text=None, skip_send=True, status="render_error")

                # ── Mark summary_shown after show_summary render ──────────────
                if _render_action == "show_summary" and not getattr(conv, "summary_shown", False):
                    try:
                        await conversation_service.update_order_field(db, conv.id, "summary_shown", True)
                        conv.summary_shown = True
                    except Exception as exc:
                        logger.error("summary_shown update error: %s", exc)

            else:
                # ── SLICE 6: LLM call routing / tiered cascade (delegated) ───────
                _routing_outcome = await run_llm_routing(
                    db, conv, client, user_text, stage, language, history_dicts,
                    system_prompt, catalogue_context, _canonical_browse_products,
                    pinned_product, variant_info, _pick_just_resolved,
                    _llm_budget, _llm_calls_today, _name_match_count,
                    _multi_match_this_turn,
                    catalogue_products=catalogue_products,
                )
                ai_reply = _routing_outcome.text
                if _routing_outcome.llm_called:
                    _llm_called_this_turn = True
                    _llm_usage = _routing_outcome.llm_usage
    except Exception as exc:
        logger.error("AI processing error: %s", exc)
        _busy_msg = "Sorry, I'm a bit busy right now — please try again in a moment, or contact us directly."
        try:
            await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
            await conversation_service.save_message(db, conv.id, "assistant", _busy_msg)
        except Exception:
            pass
        return PipelineResult(text=_busy_msg)

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
            # Guard replies are hand-built strings, not LLM output, so they must be
            # localized via language_templates same as every other deterministic
            # path — otherwise a Gujarati/Hindi customer gets dropped into English
            # the moment this safety net fires.
            from app.services.language_templates import get_template as _get_guard_tpl
            _guard_lang = getattr(conv, "last_customer_language", None) or language or "english"
            if pinned_product and getattr(conv, "pending_product_sku", None):
                _pn = pinned_product.name or conv.pending_product_sku
                _psku = getattr(pinned_product, "sku", None) or conv.pending_product_sku
                _pp = int(getattr(pinned_product, "price", 0) or 0)
                _av_colors = variant_info.get("available_colors", [])
                _av_sizes = variant_info.get("available_sizes", [])
                _color_part = (
                    _get_guard_tpl(_guard_lang, "available_colors_suffix", colors=", ".join(_av_colors))
                    if _av_colors else ""
                )
                _size_part = (
                    _get_guard_tpl(_guard_lang, "available_sizes_suffix", sizes=", ".join(_av_sizes))
                    if _av_sizes else ""
                )
                ai_reply = _get_guard_tpl(
                    _guard_lang, "pinned_availability",
                    name=_pn, sku=_psku, price=f"{_pp:,}",
                    color_part=_color_part, size_part=_size_part,
                )
                logger.info(
                    "Browsing-stage guard: pinned product %r → deterministic availability reply (conv=%s lang=%s)",
                    _psku, conv.id, _guard_lang,
                )
            else:
                _biz = (getattr(client, "business_name", None) or "our store") if client else "our store"
                ai_reply = _get_guard_tpl(_guard_lang, "which_item", business=_biz)
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
                    # FIX 1+2: same context-aware fallback for address-leak guard.
                    # Localized the same way as the transactional-output guard above —
                    # this branch was still hardcoding English (BUG: Gujarati/Hindi
                    # customers fell back to English replies whenever this safety net fired).
                    from app.services.language_templates import get_template as _get_guard_tpl2
                    _guard_lang2 = getattr(conv, "last_customer_language", None) or language or "english"
                    if pinned_product and getattr(conv, "pending_product_sku", None):
                        _pn = pinned_product.name or conv.pending_product_sku
                        _psku = getattr(pinned_product, "sku", None) or conv.pending_product_sku
                        _pp = int(getattr(pinned_product, "price", 0) or 0)
                        _av_colors = variant_info.get("available_colors", [])
                        _av_sizes = variant_info.get("available_sizes", [])
                        _color_part = (
                            _get_guard_tpl2(_guard_lang2, "available_colors_suffix", colors=", ".join(_av_colors))
                            if _av_colors else ""
                        )
                        _size_part = (
                            _get_guard_tpl2(_guard_lang2, "available_sizes_suffix", sizes=", ".join(_av_sizes))
                            if _av_sizes else ""
                        )
                        ai_reply = _get_guard_tpl2(
                            _guard_lang2, "pinned_availability_guard",
                            name=_pn, sku=_psku, price=f"{_pp:,}",
                            color_part=_color_part, size_part=_size_part,
                        )
                    else:
                        _biz = (getattr(client, "business_name", None) or "our store") if client else "our store"
                        ai_reply = _get_guard_tpl2(_guard_lang2, "which_item", business=_biz)

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
        await _record_usage(db, client, conv)
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
        await lead_service.tag_lead(
            db, sender_phone, conv.id, all_messages,
            client_id=client.id if client else None,
            channel="whatsapp",
        )
    except Exception as exc:
        logger.error("Lead tagging error: %s", exc)

    # ── SLICE 7: order creation + payment / money lifecycle (delegated) ──────
    _order_outcome = await run_order_payment(
        db, conv, client, stage, _stored_stage, sender_phone, user_text, wamid,
        history_dicts, available_stock, variant_info, _find_sku_matched_products,
    )
    stage = _order_outcome.stage

    # ── Order-creation failure: never ask for money for an order that doesn't
    # exist ────────────────────────────────────────────────────────────────
    # ai_reply for this turn was rendered (and saved) BEFORE order creation was
    # attempted, so a blocked order leaves payment instructions in it. Replace
    # the text, re-render the correct slot question, and correct the stored
    # transcript so it matches what the customer actually receives.
    if _order_outcome.order_error:
        _oe = _order_outcome.order_error
        _oe_lang = getattr(conv, "last_customer_language", None) or language or "english"
        _oe_product = (
            _oe.get("product_name")
            or getattr(pinned_product, "name", None)
            or "this product"
        )
        _next_slot = conversation_flow.get_next_required_slot(conv, variant_info)
        if _oe["kind"] == "qty_exceeds_stock" and _oe.get("stock") is not None:
            ai_reply = get_template(
                _oe_lang, "quantity_exceeds_stock",
                stock=_oe["stock"], product=_oe_product,
            )
        elif _oe["kind"] == "product_not_found":
            # The pinned SKU never resolved to a real catalogue row — product/
            # order slots were already reset by the phantom-product hard block
            # above. Never surface a fake summary/payment/success message for
            # this; tell the customer plainly and invite a fresh search.
            _PRODUCT_NOT_FOUND_MSG = {
                "english": "Sorry, we couldn't find that product in our catalogue. What would you like to order?",
                "hindi": "Maafi, yeh product humare catalogue mein nahi mila. Aap kya order karna chahenge?",
                "gujarati": "Maaf karo, aa product amara catalogue ma nathi malyo. Tame shu order karva mangho cho?",
            }
            if _oe_lang in ("hindi_roman", "hindi_devanagari", "hinglish"):
                ai_reply = _PRODUCT_NOT_FOUND_MSG["hindi"]
            elif _oe_lang in ("gujarati_roman", "gujarati_script"):
                ai_reply = _PRODUCT_NOT_FOUND_MSG["gujarati"]
            else:
                ai_reply = _PRODUCT_NOT_FOUND_MSG["english"]
        else:
            # Unclassified failure — fall back to re-asking the pending slot so
            # the customer is always given a clear next step, never silence.
            _oe_slot_q = _build_slot_question(
                _next_slot, conv, variant_info, _oe_lang,
                customer_profile=customer_profile,
                accepts_cod=getattr(client, "accepts_cod", False) if client else False,
                available_stock=available_stock,
                product_name=_oe_product,
            )
            ai_reply = _oe_slot_q or get_template(_oe_lang, "cancel_ack")
        logger.warning(
            "Order-error reply override: conv=%s kind=%s requested_qty=%s stock=%s "
            "— payment text suppressed, re-asking slot=%r",
            conv.id, _oe["kind"], _oe.get("requested_qty"), _oe.get("stock"), _next_slot,
        )
        try:
            await conversation_service.replace_last_assistant_message(db, conv.id, ai_reply)
        except Exception as exc:
            logger.error("Order-error transcript correction failed: %s", exc)

    # Bank transfer: details were previously sent as a separate whatsapp_service
    # call from webhook.py immediately after order creation (run_order_payment()
    # itself never calls whatsapp_service — see OrderOutcome's docstring for the
    # hand-off). Now built as plain text here and attached to the PipelineResult's
    # pre_texts so the adapter sends it BEFORE the main ai_reply, preserving the
    # original send order.
    _pre_texts: list[str] = []
    if (
        _order_outcome.order_created
        and getattr(_order_outcome.order, "payment_method", None) == "bank_transfer"
    ):
        _bt_text = _build_bank_transfer_text(
            amount_inr=_order_outcome.order.total_amount,
            order_number=_order_outcome.order.order_number,
            client=client,
        )
        if _bt_text:
            _pre_texts.append(_bt_text)

    # ── SLICE 8: button/list/text dispatch decision (channel-neutral) ────────
    # Only WhatsApp supports interactive messages; Instagram always gets text.
    _send_instruction = await decide_send_instruction(
        db, conv, client, stage, ai_reply, _next_slot, ctx.is_whatsapp, pinned_product,
    )
    if _pre_texts:
        _send_instruction.pre_texts = _pre_texts
    if _pending_product_images:
        _send_instruction.images = list(_pending_product_images)

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

    # The caller's channel adapter (e.g. app/routers/_whatsapp_adapter.py) is
    # responsible for resolving pid and actually sending this PipelineResult —
    # this orchestrator never calls any channel send API directly.
    return _send_instruction

