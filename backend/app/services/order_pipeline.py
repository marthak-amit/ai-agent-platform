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

import logging
import re as _re_addr
import secrets
from datetime import datetime

from app.services import conversation_service

# NOTE: deliberately uses the webhook router's logger name (not __name__) so
# log lines emitted by helpers moved out of webhook.py during the strangler-fig
# refactor are indistinguishable from pre-refactor output — several
# characterization tests assert on caplog records under logger
# "app.routers.webhook" specifically (e.g. tests/replay/test_characterization_gaps.py).
# Revisit this once all callers have moved off the legacy logger name.
logger = logging.getLogger("app.routers.webhook")

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
