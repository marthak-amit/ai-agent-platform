"""
Channel-neutral WhatsApp/Instagram order-handling pipeline.

This module is being incrementally extracted (strangler-fig) from the
previously fully-inlined logic in `app/routers/webhook.py`'s
`receive_message` handler. The goal is a single channel-neutral entry
point, `handle_inbound_message(ctx) -> PipelineResult`, that contains
all order-flow business logic with no direct calls to any channel's
send API (e.g. `whatsapp_service.send_*`). Channel-specific adapters
(currently only `app/routers/webhook.py` for WhatsApp) build the neutral
`InboundContext`, call this pipeline, and translate the returned
`PipelineResult` into channel-specific API calls.

SLICE 1 (this commit): pure, no-I/O helper functions and their
supporting constants/regexes moved here verbatim from webhook.py.
`webhook.py` now imports these names back from this module so all
existing call sites continue to work unchanged.
"""

import json as _json
import logging
import re
import re as _re_addr
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from app.services import catalogue_service, conversation_flow, conversation_service, customer_service

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
# previously did `await whatsapp_service.send_text_message(...); return` inline
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
    """One row in a WhatsApp list message: id + display title."""

    id: str
    title: str


@dataclass
class PipelineResult:
    """
    Channel-neutral outcome of processing one inbound message.

    text:         Plain-text reply, or None when nothing should be sent
                  (e.g. silent save during human takeover).
    buttons:      Quick-reply buttons to render, if any.
    list_options: List-message rows to render, if any.
    images:       List of (image_url, caption) tuples to send before/after text.
    nonce:        New button nonce to persist, if the adapter needs to know it.
    skip_send:    True when the pipeline already decided nothing should be
                  sent to the customer at all (distinct from text=None with a
                  send still expected to happen, which doesn't occur today).
    """

    text: str | None
    buttons: list[ButtonSpec] | None = None
    list_options: list[ListOptionSpec] | None = None
    images: list[tuple[str, str]] | None = None
    nonce: str | None = None
    skip_send: bool = False

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

    for options in (colors, sizes, materials):
        for opt in options:
            if opt and opt.lower() in lower:
                return f"Yes, {opt} is available for {prod_name}."

    cleaned = lower.replace("?", " ")
    for word in cleaned.split():
        word = word.strip(".,!")
        if word in _KNOWN_COLOR_WORDS and word not in {c.lower() for c in colors}:
            opts = ", ".join(colors) if colors else "see our catalogue"
            return f"{word.capitalize()} isn't available for {prod_name}. Available colors: {opts}."

    # No specific attribute named — fall back to overall stock state.
    if available_stock is not None:
        if available_stock <= 0:
            return f"Sorry, {prod_name} is currently out of stock."
        return f"Yes, {prod_name} is available."
    return ""


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
_SLOT_ATTEMPT_ESCAPE_HATCH = 4   # append escape hatch at this attempt number
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
#   await whatsapp_service.send_text_message(sender_phone, reply)
#   return {"status": "ok"}
# Converted to: persist whatever DB state the original code persisted, then
# return a PipelineResult instead of sending. webhook.py's adapter is
# responsible for actually calling whatsapp_service.send_text_message with
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
    Cancel-in-payment guard: handle "cancel" tapped/typed while stage="payment"
    (pending UPI confirmation) — cancels the pending_payment Order, resets
    order slots, sets stage back to "greeting".

    record_usage: the webhook module's _record_usage(db, client) callable,
    passed in rather than imported, since it stays defined in webhook.py.
    """
    if not (user_text.lower().strip() == "cancel" and stored_stage == "payment"):
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
    logger.info("conv=%s PAYMENT-STAGE CANCEL — order cancelled, slots reset, stage=greeting", conv.id)
    try:
        await conversation_service.save_message(db, conv.id, "user", user_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", _pc_reply)
    except Exception as exc:
        logger.error("Payment-stage cancel save error: %s", exc)
    try:
        await record_usage(db, client)
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
                            await record_usage(db, client)
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
            if not _is_foreign_sku_ref:
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
                    await record_usage(db, client)
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
    _any_slot_filled = bool(
        getattr(conv, "selected_color", None)
        or getattr(conv, "selected_size", None)
        or getattr(conv, "selected_material", None)
        or (getattr(conv, "pending_order_quantity", None) or 0)
        or getattr(conv, "customer_name", None)
        or getattr(conv, "delivery_address", None)
    )
    _browsing_stage_pinned = stored_stage in (
        "product_inquiry", "qualification", "objection_handling", "offer_making", "greeting"
    )
    _is_bare_attribute = len(user_text.split()) == 1
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
            or (_browsing_stage_pinned and _is_bare_attribute)
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
                                await record_usage(db, client)
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
