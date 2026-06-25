"""
Conversation flow engine — stage detection and stage-specific prompt instructions.

Stages model the customer's journey from first contact to completed order.
detect_stage() uses the AI to classify where the conversation currently sits,
then get_stage_instructions() returns focused guidance for that stage.
"""

from __future__ import annotations

import logging
import re
import unicodedata as _ud

from collections import OrderedDict

from app.services import cost_log

logger = logging.getLogger(__name__)

# Tier 2 cost-cascade cache: normalized (next_slot, phrase) → classified intent
# dict, checked before every cheap-classify Groq call so repeated phrases
# ("yes", "ok", "haan") cost ₹0 after the first occurrence. Capped at
# settings.classify_cache_size (LRU eviction via OrderedDict.move_to_end).
_classify_cache: "OrderedDict[tuple[str, str], dict]" = OrderedDict()


def _classify_cache_get(key: tuple[str, str]) -> dict | None:
    """Return a cached intent result for *key*, moving it to most-recently-used."""
    hit = _classify_cache.get(key)
    if hit is not None:
        _classify_cache.move_to_end(key)
    return hit


def _classify_cache_put(key: tuple[str, str], value: dict, max_size: int) -> None:
    """Store *value* under *key*, evicting the oldest entry once over *max_size*."""
    _classify_cache[key] = value
    _classify_cache.move_to_end(key)
    while len(_classify_cache) > max_size:
        _classify_cache.popitem(last=False)


def _log_groq_usage(conversation_id: int | None, call_kind: str, model: str, resp, text: str) -> None:
    """
    Append a classify/extract Groq call to the per-conversation cost log so it
    is visible in the cost report instead of being silently uncounted.

    Args:
        conversation_id: PK of the Conversation row, or None to skip logging.
        call_kind:       'classify' or 'extract'.
        model:           Groq model name used for this call.
        resp:             The chat.completions.create() response object.
        text:            The inbound customer text that triggered this call.
    """
    if conversation_id is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    cost_log.log(
        conversation_id, "IN", text,
        path="LLM", model=model,
        in_tok=usage.prompt_tokens, out_tok=usage.completion_tokens,
        call_kind=call_kind,
    )

# Matches a standalone SKU token (2–4 letters + 4–6 digits), same as catalogue_service.SKU_PATTERN.
# Also accepts a single optional space between the letter-prefix and digits (voice-transcription tolerance).
_SKU_ONLY_PATTERN = re.compile(r"^[A-Za-z]{2,4}\s?\d{4,6}$")

# Words that must never be saved as a customer name.
# Includes command/reset words the test simulator sends, common short tokens,
# and anything that cannot be a real person's name.
# Regex patterns to strip filler prefixes before validating a customer name.
# Order matters: longest/most-specific first so "sure, I am" is stripped before "I am".
_NAME_FILLER_PREFIXES: tuple[str, ...] = (
    # English
    r"sure[,\s]+i\s+am\s+",
    r"sure[,\s]+",
    r"ok[,\s]+i\s+am\s+",
    r"okay[,\s]+i\s+am\s+",
    r"yes[,\s]+i\s+am\s+",
    r"i\s+am\s+",
    r"i'm\s+",
    r"my\s+name\s+is\s+",
    r"this\s+is\s+",
    r"it's\s+",
    r"its\s+",
    r"call\s+me\s+",
    # Hindi
    r"mera\s+naam\s+(?:hai\s+)?",
    r"mera\s+name\s+(?:hai\s+)?",
    r"naam\s+(?:hai\s+)?",
    # Gujarati
    r"maru\s+naam\s+(?:chhe\s+)?",
    r"mara\s+naam\s+(?:che\s+)?",
    r"tamaru\s+naam\s+(?:chhe\s+)?",
)
_NAME_FILLER_RE = re.compile(
    r"^(?:" + "|".join(_NAME_FILLER_PREFIXES) + r")",
    re.IGNORECASE,
)


def _strip_name_prefixes(text: str) -> str:
    """Strip common filler prefixes before extracting a name candidate."""
    return _NAME_FILLER_RE.sub("", text.strip()).strip()


COMMAND_WORDS: frozenset[str] = frozenset({
    "reset", "quit", "exit", "demo", "sales",
    "image", "help", "start", "stop", "test",
    "/reset", "/quit", "/demo", "/start", "/stop",
    "hi", "hello", "namaste", "haan", "ha", "nahi", "okay", "ok",
    "yes", "no", "paid", "done", "sent", "transferred", "completed",
    "cod", "upi", "gpay", "phonepay", "paytm", "phonepe",
    "s", "m", "l", "xl", "xxl", "xs", "xxxl",
    "red", "blue", "green", "pink", "navy", "yellow", "white", "black",
    "purple", "orange", "maroon", "gold", "silver", "beige", "brown",
})


def is_valid_name(text: str) -> bool:
    """
    Return True only when *text* looks like a genuine customer name.

    A valid name must have at least 2 characters, contain no digits,
    not be a command/reset word, and not start with '/'.
    """
    stripped = text.strip().lower()
    if len(stripped) < 2:
        return False
    if stripped in COMMAND_WORDS:
        return False
    if stripped.startswith("/"):
        return False
    if stripped.isdigit():
        return False
    return True


_ADDRESS_STOP_WORDS: frozenset[str] = frozenset({
    "hello", "hi", "hey", "yes", "no", "ok", "okay", "ha", "haan",
    "paid", "cancel", "change", "thanks", "thank you", "done",
    "shukriya", "dhanyavaad", "theek", "sahi",
})

_ADDRESS_QUESTION_RE = re.compile(
    r"^(how|what|why|when|where|which|who|kya|kem|ketla|kitna|kitne)\b",
    re.IGNORECASE,
)

# P1-6: filler prefixes that get typed alongside a real address and must be
# stripped before storing ("okay, 800 somerest, ahmedabad, 350012" → the
# "okay," part is not part of the address).
_ADDRESS_FILLER_RE = re.compile(
    r"^(okay|ok|yes|sure|haan|ha|change( it)?( to)?|its|it's)[ ,:-]*",
    re.IGNORECASE,
)

# A genuine Indian delivery address must include a 6-digit pincode.
_PINCODE_RE = re.compile(r"\b\d{6}\b")

# "use my old address" / "same as before" / "previous address" — reuse the
# customer's saved profile address instead of rejecting as an invalid address.
_REUSE_OLD_ADDRESS_RE = re.compile(
    r"\b(old|previous|same|earlier)\b.*\baddress\b|\baddress\b.*\b(old|previous|same|earlier)\b"
    r"|\bsame as before\b",
    re.IGNORECASE,
)

def clean_address(text: str) -> str:
    """Strip leading filler ("okay,", "sure,", "change to" etc.) from a raw address string."""
    return _ADDRESS_FILLER_RE.sub("", text.strip()).strip().strip(",").strip()


def classify_address_rejection(text: str) -> str:
    """
    Diagnose WHY is_valid_address(text) failed, so the customer can be told
    the specific thing to fix instead of a generic "incomplete address".

    Returns one of: "too_short", "no_structure", "bad_pincode". Callers should
    only call this when is_valid_address(text) already returned False.
    """
    stripped = clean_address(text)
    if len(stripped) < 10:
        return "too_short"
    if not any(c.isdigit() for c in stripped) and "," not in stripped:
        return "no_structure"
    _digit_runs = re.findall(r"\d+", stripped)
    if _digit_runs:
        _longest = max(_digit_runs, key=len)
        if len(_longest) > 6 or (len(_longest) in (4, 5) and not _PINCODE_RE.search(stripped)):
            return "bad_pincode"
    return "too_short"  # fallback — shouldn't be reached if is_valid_address rejected it


def is_valid_address(text: str) -> bool:
    """
    Return True only when *text* looks like a genuine delivery address.

    Rejects:
    - Shorter than 10 characters (after stripping filler)
    - Contains no digit AND no comma (minimal address structure)
    - Is a question (contains '?' or starts with a question word)
    - Matches a stop-word (greeting, ack, command)
    - Is a pure change/edit intent ("change it to...")
    - Has no 6-digit pincode
    """
    stripped = clean_address(text)
    if len(stripped) < 10:
        return False
    lower = stripped.lower()
    first_word = lower.split()[0] if lower.split() else ""
    if lower in _ADDRESS_STOP_WORDS or first_word in _ADDRESS_STOP_WORDS:
        return False
    if "?" in stripped:
        return False
    if _ADDRESS_QUESTION_RE.match(stripped):
        return False
    if not any(c.isdigit() for c in stripped) and "," not in stripped:
        return False
    # A pincode-shaped digit run (if present) must be exactly 6 digits — reject
    # an obviously truncated/garbled one (e.g. "3501" or "35001234567").
    # Addresses with no pincode at all are still accepted (existing behaviour).
    _digit_runs = re.findall(r"\d+", stripped)
    if _digit_runs:
        _longest = max(_digit_runs, key=len)
        if len(_longest) > 6 or (len(_longest) in (4, 5) and not _PINCODE_RE.search(stripped)):
            return False
    return True


# A valid Indian mobile number: 10 digits, first digit 6-9 (optionally
# prefixed with a +91/91/0 country code, with spaces/hyphens stripped).
_MOBILE_DIGITS_RE = re.compile(r"^(?:\+?91|0)?([6-9]\d{9})$")


def clean_mobile_number(text: str) -> str:
    """Strip spaces/hyphens/parens, leaving only the digits (and leading +)."""
    stripped = text.strip()
    return re.sub(r"[ \-()]", "", stripped)


def is_valid_mobile_number(text: str) -> bool:
    """
    Return True only when *text* looks like a genuine 10-digit Indian mobile
    number, optionally with a +91/91/0 prefix. Rejects garbage input the same
    way is_valid_address() rejects garbage addresses — reject anything that
    isn't a clean digit run of the right shape.
    """
    cleaned = clean_mobile_number(text)
    return bool(_MOBILE_DIGITS_RE.match(cleaned))


def normalize_mobile_number(text: str) -> str:
    """Return the bare 10-digit mobile number, stripping any country-code prefix."""
    cleaned = clean_mobile_number(text)
    match = _MOBILE_DIGITS_RE.match(cleaned)
    return match.group(1) if match else cleaned


STAGES: dict[str, dict] = {
    "greeting": {
        "description": "Customer just started",
        "goal": "Warm welcome + understand need",
        "next": "product_inquiry",
    },
    "product_inquiry": {
        "description": "Customer asking about products",
        "goal": "Show relevant products, build interest",
        "next": "objection_handling OR qualification",
    },
    "qualification": {
        "description": "Customer showing interest",
        "goal": "Understand budget, quantity, timeline",
        "next": "offer_making",
    },
    "objection_handling": {
        "description": "Customer has concerns",
        "goal": "Address concern, rebuild interest",
        "next": "qualification OR offer_making",
    },
    "offer_making": {
        "description": "Ready to buy",
        "goal": "Present clear offer, create urgency",
        "next": "order_collection",
    },
    "order_collection": {
        "description": "Customer agreed to buy — slot machine collecting details",
        "goal": "Collect quantity, variants, name, address, payment one slot at a time",
        "next": "awaiting_final_confirmation",
    },
    "awaiting_final_confirmation": {
        "description": "All order details collected — waiting for explicit yes/no",
        "goal": "Show summary, get customer's explicit confirmation",
        "next": "completed",
    },
    "payment": {
        "description": "Order details collected",
        "goal": "Send payment QR, confirm order",
        "next": "completed",
    },
    "completed": {
        "description": "Order placed",
        "goal": "Thank customer, ask for review",
        "next": None,
    },
    "off_topic": {
        "description": "Customer went off topic",
        "goal": "Politely redirect to business",
        "next": "product_inquiry",
    },
}

_VALID_STAGES = set(STAGES.keys())

# Affirmative / negative keywords for the awaiting_final_confirmation stage.
_CONFIRMATION_YES = frozenset({
    "yes", "haan", "han", "ha", "confirm", "ok", "okay", "sahi", "theek",
    "bilkul", "zaroor", "done", "proceed", "place", "book",
    "1",  # numbered choice: 1 = Confirm & pay
    # Devanagari (Hindi) affirmatives — detect_stage splits on whitespace then intersects
    "हाँ", "हां", "ठीक",
    # Gujarati script affirmatives
    "હા", "બરાબર", "કરો",
})
_CONFIRMATION_NO = frozenset({
    "no", "nahi", "nope", "cancel", "change", "badal", "nhi",
    "different", "wrong", "incorrect",
    "3",  # numbered choice: 3 = Cancel (when cross-sell shown)
    "2",  # numbered choice: 2 = Cancel (when no cross-sell, 2 is Cancel)
    # Devanagari (Hindi) negatives
    "नहीं", "ना", "मत",
    # Gujarati script negatives
    "ના", "નહીં", "નહિ", "રદ",
})
# "2" when cross-sell is shown means "add cross-sell product" — webhook.py intercepts
# this before detect_stage runs. If no cross-sell was shown, 2 maps to Cancel above.

# Words that mean "I've paid" — when detected in payment stage, move to completed.
PAYMENT_CONFIRMATION_WORDS = {
    "paid", "done", "sent", "transferred", "ho gaya", "kar diya",
    "completed", "finished", "payment done", "bhej diya", "upi kiya",
    "gpay kiya", "phonepay kiya", "payment kiya", "kiya", "payment ho gaya",
    "bhugtan", "transfer", "payment kar diya",
    # Bare app names used as "I paid via X"
    "gpay", "phonepay", "paytm", "phonepe",
    # Additional confirmation phrases
    "payment kiya", "kar diya", "ho gaya", "bhej diya",
    "upi kiya", "payment ho gaya", "payment kar diya",
}

# Stages considered "browsing" (Mode A) — transition to order_collection only on
# explicit purchase affirmation after a product was shown.
_BROWSING_STAGES: frozenset[str] = frozenset({
    "greeting", "product_inquiry", "qualification",
    "objection_handling", "offer_making",
})

# Keywords in the AI's LAST message meaning it just offered to take an order
# for a specific product (Mode A offer hook).
_ORDER_OFFER_CUES: tuple[str, ...] = (
    "order karein", "order karna chahenge", "would you like to order",
    "order karo", "order karna hai", "place an order", "order chahiye",
    "order dena chahenge", "book karna", "khareedna chahte", "lena chahenge",
    "abhi order", "order karoge", "order dein", "lenge?", "order lena?",
    "order karein?", "khareedenge?",
)

# Customer words that explicitly affirm purchase intent
_PURCHASE_AFFIRMATION: frozenset[str] = frozenset({
    "yes", "haan", "han", "ha", "ok", "okay", "sahi", "theek",
    "bilkul", "zaroor", "done", "proceed", "place", "book",
    "order", "chahiye", "le lo", "le lena", "lena hai", "kar do",
    "kardo", "confirm", "bhejo", "lena", "dedo", "de do",
})

# Customer words that decline the order offer (stay browsing, don't escalate to objection_handling)
_DECLINE_ORDER: frozenset[str] = frozenset({
    "no", "nahi", "nope", "nhi", "not", "mat",
})

# Keywords in a customer message that request the catalogue / shop link
CATALOGUE_KEYWORDS: tuple[str, ...] = (
    "catalogue", "catalog", "catelog", "cateloge", "catlouge", "cataloge",
    "full list", "sab dikhao", "shop link",
    "all products", "poora list", "dekhna hai sab", "sab products",
    "collection dekha", "website link", "shop website", "store link",
    "puri list", "all items", "sab kuch dikhao",
)


def is_catalogue_request(text: str) -> bool:
    """Return True if the customer is asking for the full catalogue / shop link."""
    lower = text.lower()
    return any(kw in lower for kw in CATALOGUE_KEYWORDS)


def _last_ai_offered_order(conversation_history: list[dict]) -> bool:
    """
    True if the agent's most recent message offered to place an order for a
    specific product (Mode A offer hook — "order karein?" etc.).

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts,
                              most recent last.

    Returns:
        True when the last assistant turn contains an order-offer cue.
    """
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _ORDER_OFFER_CUES)
    return False


_STAGE_DETECT_PROMPT = """You are a sales conversation stage classifier.

Given the conversation history and the latest customer message, return EXACTLY ONE of these stage names:
greeting, product_inquiry, qualification, objection_handling, offer_making, order_collection, payment, completed, off_topic

Stage definitions:
- greeting: First message, no clear intent yet
- product_inquiry: Asking about products, prices, availability
- qualification: Showing interest, asking about quantity/timeline/specs
- objection_handling: Expressing hesitation, price concern, "sochna hai", "mehnga hai"
- offer_making: Showing clear buying intent, ready to decide
- order_collection: Actively providing order details (name, address, quantity)
- payment: Order confirmed, arranging payment method
- completed: Payment done, order placed
- off_topic: Asking about something unrelated to the business

Respond with ONLY the stage name. Nothing else."""


# Cues in the AGENT's last message that mean it just asked for an
# order-collection detail (quantity/name/address/payment/summary). If the
# customer is replying to one of these, the conversation is — by definition —
# in order_collection, regardless of what the AI classifier guesses. This
# stops the strict order_collection sequence from being skipped just because
# the LLM-based stage classifier mislabels the turn (e.g. as "product_inquiry").
_ORDER_FOLLOWUP_CUES = (
    "kitne piece", "kitne pieces", "ketla piece", "ketla pieces",
    "how many piece", "how many pieces", "quantity",
    "aapka naam", "aapka naam kya", "tamaru naam", "your name", "naam kya hai",
    "delivery address", "address kya hai", "address su che", "your address",
    "upi ya cod", "upi or cod", "ya cod", "ke cod", "cash on delivery",
    "payment kaisa", "payment kai rite", "how would you like to pay",
    "order summary", "confirm karein", "confirm?", "sab sahi hai",
)

# Keywords in the AI's last message indicating it just asked for QUANTITY.
# Quantity is ONLY extracted when the agent explicitly requested it.
_QUANTITY_ASK_CUES = (
    "how many", "kitne piece", "kitne pieces", "ketla piece", "ketla pieces",
    "quantity", "kitne chahiye", "how many pieces", "kitna chahiye",
    "kitne loge", "ketla joiye",
)

# Keywords in the AI's last message indicating it just asked for NAME+ADDRESS
# together in a single question (e.g. "May I have your name and delivery address?").
_NAME_AND_ADDRESS_CUES = (
    "name and", "naam aur", "naam aur address", "name and address",
    "naam aur delivery", "name and delivery",
)


def _last_agent_asked_quantity(conversation_history: list[dict]) -> bool:
    """
    True if the agent's most recent message explicitly asked for quantity.
    Used to guard quantity extraction so a number inside an address reply
    (e.g. "702 Somerset St") is never mistaken for a piece count.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts,
                              most recent last.

    Returns:
        True when the last assistant turn contains a quantity-ask cue.
    """
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _QUANTITY_ASK_CUES)
    return False


def _last_agent_asked_name_and_address(conversation_history: list[dict]) -> bool:
    """
    True if the agent's most recent message asked for name AND address together
    (e.g. "May I have your name and delivery address?"). When this is the case,
    a combined reply like "Amit, 702 Somerset, CA" should be split into name +
    address — never parsed for quantity.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts,
                              most recent last.

    Returns:
        True when the last assistant turn contains a combined name+address cue.
    """
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _NAME_AND_ADDRESS_CUES)
    return False


_SAVED_ADDRESS_AFFIRMATIONS: frozenset[str] = frozenset({
    "yes", "ye", "ya", "yep", "yup",
    "haan", "han", "ha",
    "ok", "okay", "sure", "fine",
    "correct", "sahi", "bilkul", "theek",
})

# Affirmative set shared across all confirm-style slot prompts.
_SLOT_AFFIRMATIVES: frozenset[str] = frozenset({
    "yes", "ye", "yeah", "y",
    "haan", "ha", "han", "ha",
    "ok", "okay", "sahi", "theek",
    "bilkul", "zaroor", "sure", "fine", "correct",
})

# Words meaning "I want to change / use a different address".
# When detected in response to a saved-address confirm, the slot is NOT filled
# so the next question asks for a fresh address.
_SAVED_ADDRESS_NEGATIONS: frozenset[str] = frozenset({
    "change", "no", "nahi", "nope", "edit", "badal", "nhi",
    "different", "new", "alag", "nayi",
})


# Shared deterministic (Tier 0, no LLM) vocabulary for classifying a reply to
# the saved-address confirm prompt ("{name}, deliver to: {address}? (yes/change)").
_NEGATION_WORDS: frozenset[str] = frozenset({"not", "don't", "dont", "no", "nahi", "mat"})
_NEGATION_PHRASES: tuple[str, ...] = ("do not", "no need")

_ADDRESS_CONFIRM_TOKENS: frozenset[str] = frozenset({
    "yes", "y", "ok", "okay", "k", "haan", "han", "ha",
    "confirm", "correct", "same", "right", "theek", "sahi",
})
_ADDRESS_CHANGE_TOKENS: frozenset[str] = frozenset({
    "change", "edit", "new", "different", "update", "badlo", "badal", "naya",
})


def is_negated(text: str) -> bool:
    """
    Shared negation guard — True if the message contains an explicit negation
    word/phrase ("not", "don't", "do not", "no need", "nahi", "mat", ...).

    Used to avoid inverting intent on phrases like "I do not want to change
    the address", where a naive keyword match on "change" alone would wrongly
    start a change-address flow.
    """
    lower = f" {text.lower().strip()} "
    if any(f" {w} " in lower for w in _NEGATION_WORDS):
        return True
    return any(p in text.lower() for p in _NEGATION_PHRASES)


def classify_address_confirmation(text: str) -> str:
    """
    Deterministic (Tier 0, no LLM, no address validation) classifier for a
    reply to the saved-address confirm prompt.

    Returns one of "confirm", "change", "unclear":
      - "confirm": keep the saved address (explicit yes/ok/... OR a change
        token that is negated, e.g. "I do not want to change the address").
      - "change":  customer wants to enter a different address.
      - "unclear": garbage/unrelated reply (e.g. "Hi") — caller must re-prompt,
        never run address validation on it.
    """
    lower = text.lower().strip()
    words = re.findall(r"[a-zA-Z']+", lower)
    word_set = set(words)

    has_change = bool(word_set & _ADDRESS_CHANGE_TOKENS)
    has_confirm = bool(word_set & _ADDRESS_CONFIRM_TOKENS)

    if has_change and is_negated(text):
        return "confirm"
    if has_confirm:
        return "confirm"
    if has_change:
        return "change"
    return "unclear"


def _last_agent_offered_saved_address(conversation_history: list[dict]) -> bool:
    """True if the most recent agent message offered a saved address for confirmation (Deliver to X? yes/change)."""
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return (
                "deliver to" in text
                or "(yes/change)" in text
                or "yes/change" in text
                or "use this address" in text
            )
    return False


def _last_agent_offered_payment(conversation_history: list[dict]) -> bool:
    """True if the most recent agent message was a payment-method prompt."""
    _PAYMENT_CUES = (
        "how would you like to pay", "upi se denge", "kem bharvu",
        "payment:", "pay via", "upi or", "upi ya",
        "confirm? (yes)", "(yes)",
    )
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _PAYMENT_CUES)
    return False


def _last_agent_offered_upi_only(conversation_history: list[dict]) -> bool:
    """True if the most recent agent message was a UPI-only payment prompt (no COD option)."""
    _PAYMENT_CUES = (
        "how would you like to pay", "upi se denge", "kem bharvu",
        "payment:", "pay via", "upi?",
        "confirm? (yes)",
    )
    _COD_CUES = ("cod", "cash on delivery", "cash", "delivery pe payment")
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            has_payment_cue = any(cue in text for cue in _PAYMENT_CUES)
            has_upi = "upi" in text
            has_cod = any(cue in text for cue in _COD_CUES)
            return (has_payment_cue or has_upi) and not has_cod
    return False


def _is_order_followup(conversation_history: list[dict]) -> bool:
    """
    True if the agent's most recent message asked for an order-collection
    detail (quantity, name, address, payment, or showed the order summary),
    meaning the customer's reply is necessarily part of order collection.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts,
                              most recent last.

    Returns:
        True if the last assistant turn looks like an order-collection prompt.
    """
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _ORDER_FOLLOWUP_CUES)
    return False


# Keywords that indicate a price objection / discount request mid-order.
# Checked before the Groq call so common cases cost zero tokens.
_PRICE_OBJECTION_KW: tuple[str, ...] = (
    "price vadhare", "bahut mehenga", "too expensive", "very expensive",
    "discount", "kam karo", "sasta", "sasta karo", "mehenga", "mahnga",
    "costly", "cheap karo", "price kam", "rate kam", "concession",
    "offer karo", "offer hai", "koi offer", "thoda kam", "reduce",
    "lower price", "price ghata", "ghata do", "less price", "price less",
    "itna mehnga", "aata matha",  # Gujarati: "too expensive"
    "vhatu che", "ghanu mahghu",
)


def _is_price_objection(text: str) -> bool:
    """Return True if the message is a price complaint / discount request."""
    lower = text.lower()
    return any(kw in lower for kw in _PRICE_OBJECTION_KW)


async def classify_user_intent(
    user_text: str,
    next_slot: str,
    pinned_product_name: str,
    conversation_id: int | None = None,
) -> dict:
    """
    Lightweight intent classifier — called before slot extraction when the
    conversation is in order_collection/awaiting_final_confirmation.

    Returns the DOC A {intent, entities} struct. The LLM is the ONLY source of
    customer-facing classification in order stages; it never generates a reply.

    intent is one of: ANSWER | NEW_PRODUCT | CANCEL | DISCOUNT_QUERY | OFF_TOPIC | OTHER.
    entities is a sparse dict of any tokens visible in the message (e.g. {"sku": "SR123"}).

    DISCOUNT_QUERY is returned (without a Groq call) when the message contains
    obvious price-objection / discount keywords.

    Args:
        user_text:           The customer's latest message.
        next_slot:           The slot currently being collected.
        pinned_product_name: Display name of the product currently being ordered.

    Returns:
        {"intent": str, "entities": dict} — intent falls back to "ANSWER" on any error.
    """
    # Fast keyword path — no LLM call needed for obvious price complaints.
    if _is_price_objection(user_text):
        return {"intent": "DISCOUNT_QUERY", "entities": {}}

    # Fast keyword path for cancel — no LLM call needed.
    _CANCEL_KW = (
        "cancel", "ruk jao", "band karo", "rok do",
        "રદ કરો", "रद्द", "cancel karo", "nahi chahiye", "nahi karna",
    )
    _text_lower = user_text.lower().strip()
    if any(kw in _text_lower for kw in _CANCEL_KW):
        return {"intent": "CANCEL", "entities": {}}

    # Fast SKU path — a SKU token is never a valid answer to most order slots.
    # Skipped for free-text slots (name/address) where a coincidental SKU-shaped
    # substring is common and expected — e.g. a street address with a pincode
    # ("42 MG Road, Pune 411001" → "PUNE411001") must not be mistaken for a
    # product switch.
    if next_slot not in ("delivery_address", "customer_name"):
        from app.services.catalogue_service import extract_skus_from_text as _extract_skus
        _sku_hits = _extract_skus(user_text)
        if _sku_hits:
            return {"intent": "NEW_PRODUCT", "entities": {"sku": _sku_hits[0]}}

    from openai import AsyncOpenAI
    from app.config import get_settings

    settings = get_settings()

    # Tier 2 cache: a normalized phrase classifies to the same intent for the
    # same slot/product context, so check before spending a Groq call.
    _cache_key = (next_slot or "", pinned_product_name or "", _text_lower)
    _cached = _classify_cache_get(_cache_key)
    if _cached is not None:
        logger.info("ROUTE tier=2 cache_hit=true classify intent=%s", _cached.get("intent"))
        return _cached

    prompt = (
        f"Current order: {pinned_product_name}. "
        f"We just asked the customer for: {next_slot}. "
        f"Customer replied: '{user_text}'.\n\n"
        "Classify into exactly one of:\n"
        "- ANSWER: a direct answer to the question asked\n"
        "- NEW_PRODUCT: contains a different product code/SKU or asks about a different product\n"
        "- CANCEL: wants to cancel/stop the current order\n"
        "- DISCOUNT_QUERY: asks for a discount, lower price, or complains about price\n"
        "- OFF_TOPIC: completely unrelated to the store, products, or order (e.g. 'What is Flutter?', trivia, news, tech questions) — NOT off-topic: product questions, greetings, names, addresses, sizes, colours, payment words\n"
        "- OTHER: anything else that doesn't fit above\n\n"
        "Respond with ONLY one word: ANSWER, NEW_PRODUCT, CANCEL, DISCOUNT_QUERY, OFF_TOPIC, or OTHER."
    )

    try:
        client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )
        import asyncio as _aio
        _backoff = 1.0
        for _attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=settings.classify_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=10,
                    temperature=0,
                )
                break
            except Exception as _e:
                _is_429 = "429" in str(_e) or "rate" in str(_e).lower()
                if _is_429 and _attempt < 2:
                    logger.warning("classify_user_intent 429 — backoff %.1fs (attempt %d)", _backoff, _attempt + 1)
                    await _aio.sleep(_backoff)
                    _backoff *= 2
                    continue
                raise
        _log_groq_usage(conversation_id, "classify", settings.classify_model, resp, user_text)
        logger.info("ROUTE tier=2 cache_hit=false classify model=%s", settings.classify_model)
        raw = (resp.choices[0].message.content or "").strip().upper()
        if raw in ("ANSWER", "NEW_PRODUCT", "CANCEL", "DISCOUNT_QUERY", "OFF_TOPIC", "OTHER"):
            _result = {"intent": raw, "entities": {}}
            _classify_cache_put(_cache_key, _result, settings.classify_cache_size)
            return _result
        # If response contains the keyword, extract it
        for label in ("NEW_PRODUCT", "CANCEL", "DISCOUNT_QUERY", "OFF_TOPIC", "OTHER", "ANSWER"):
            if label in raw:
                _result = {"intent": label, "entities": {}}
                _classify_cache_put(_cache_key, _result, settings.classify_cache_size)
                return _result
    except Exception as exc:
        logger.warning("classify_user_intent failed (defaulting to ANSWER): %s", exc)

    return {"intent": "ANSWER", "entities": {}}


async def classify_buy_intent(user_text: str, product_name: str, conversation_id: int | None = None) -> bool:
    """
    LLM-based buy-intent classifier for browsing stages.

    Returns True when the customer is expressing intent to purchase the pinned
    product.  No keyword lists — the model decides from the full message so
    that any natural-language phrasing ("lena hai", "le lo", "order kar do",
    "haan bhai", "2 chahiye", "yes please") is caught uniformly.

    Called only when a product is already pinned (pending_product_sku set) and
    the stage is still a browsing stage (greeting/product_inquiry/etc.).  It is
    NOT called inside order_collection — that path uses classify_user_intent.

    Args:
        user_text:    The customer's latest message.
        product_name: Display name of the pinned product.

    Returns:
        True if the customer wants to place an order for this product.
        Falls back to False on any error so the flow degrades to browsing.
    """
    if not user_text or not user_text.strip():
        return False
    # Single-character or emoji-only messages are almost never buy intent.
    if len(user_text.strip()) <= 2:
        return False

    prompt = (
        f"A customer is chatting with an online store that sells {product_name}.\n"
        f"Customer message: '{user_text}'\n\n"
        "Is the customer clearly expressing intent to BUY, ORDER, or PURCHASE "
        "this product right now?\n"
        "Answer YES only if they want to order/buy/proceed with purchase.\n"
        "Answer NO if they are asking a question, browsing, saying no/maybe/later, "
        "or the message is ambiguous.\n"
        "Respond with only YES or NO."
    )

    try:
        from openai import AsyncOpenAI
        from app.config import get_settings
        import asyncio as _aio

        settings = get_settings()
        client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )
        _backoff = 1.0
        for _attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                    temperature=0,
                )
                break
            except Exception as _e:
                _is_429 = "429" in str(_e) or "rate" in str(_e).lower()
                if _is_429 and _attempt < 2:
                    logger.warning("classify_buy_intent 429 — backoff %.1fs (attempt %d)", _backoff, _attempt + 1)
                    await _aio.sleep(_backoff)
                    _backoff *= 2
                    continue
                raise
        _log_groq_usage(conversation_id, "classify", "llama-3.1-8b-instant", resp, user_text)
        raw = (resp.choices[0].message.content or "").strip().upper()
        result = raw.startswith("YES")
        logger.debug("classify_buy_intent product=%r text=%r → %s", product_name, user_text[:60], result)
        return result
    except Exception as exc:
        logger.warning("classify_buy_intent failed (defaulting False): %s", exc)
        return False


async def is_off_topic_message(
    user_text: str,
    stage: str,
    pinned_product_name: str | None = None,
    conversation_id: int | None = None,
) -> bool:
    """
    Return True when the customer's message is completely unrelated to the store.

    Called for idle and completed stages where classify_user_intent is not used.
    Uses llama-3.1-8b-instant (fast, cheap). Falls back to False on any error
    so genuine product messages are never blocked.

    NOT off-topic: product/shopping questions, greetings, affirmations, names,
    addresses, sizes, colours, quantities, payment words, SKUs.
    OFF_TOPIC: general knowledge, tech, news, politics, trivia, personal advice.

    Args:
        user_text:           The customer's message.
        stage:               Current conversation stage.
        pinned_product_name: Optional product context.

    Returns:
        True if the message is off-topic (should be deflected).
    """
    # Very short messages are almost never off-topic (greetings, affirmations)
    if len(user_text.strip()) <= 3:
        return False

    store_context = f" in a store selling {pinned_product_name}" if pinned_product_name else " in an online store"

    prompt = (
        f"A customer{store_context} sent: '{user_text}'\n\n"
        "Is this message completely unrelated to shopping, products, orders, delivery, or payment?\n"
        "Answer YES only for: general knowledge questions, tech questions, news, politics, trivia, jokes, weather, recipes, medical advice.\n"
        "Answer NO for: product questions, shopping queries, greetings, names, addresses, sizes, colours, payment words, SKUs, anything store-related.\n"
        "Respond with only YES or NO."
    )

    try:
        from openai import AsyncOpenAI
        from app.config import get_settings

        settings = get_settings()
        _client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )
        resp = await _client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0,
        )
        _log_groq_usage(conversation_id, "classify", "llama-3.1-8b-instant", resp, user_text)
        raw = (resp.choices[0].message.content or "").strip().upper()
        return raw.startswith("YES")
    except Exception as exc:
        logger.warning("is_off_topic_message failed (defaulting False): %s", exc)
        return False


def detect_stage(
    conversation_history: list[dict],
    latest_message: str,
    stored_stage: str | None = None,
    pending_product_sku: str | None = None,
) -> str:
    """
    Classify the current conversation stage using keyword heuristics.

    Deterministic, zero-latency — no AI call. Runs an order-followup check
    first (if the agent's last message asked for name/address/quantity the
    stage is forced to 'order_collection'), then falls back to keyword matching
    on the latest customer message.

    Mode A / Mode B split:
      - Mode A (browsing): greeting, product_inquiry, qualification, etc.
        Product identification → show details + "order karein?" WITHOUT
        entering order_collection.
      - Mode B (order_collection): ONLY when the customer explicitly affirms
        purchase intent after a product was shown (pending_product_sku set),
        OR when the AI's last message already asked for an order-detail slot.

    Stage locking: once in 'payment' or 'completed', the stage never moves
    backward. Payment confirmation words ("paid", "ho gaya" etc.) in payment
    stage immediately advance to 'completed'.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts.
        latest_message:       The customer's most recent message text.
        stored_stage:         The stage persisted on the Conversation row from
                              the previous turn — used for forward-only locking.
        pending_product_sku:  SKU pinned from the previous turn (if any).
                              Used to gate Mode A → Mode B transition.

    Returns:
        One of the STAGES keys.
    """
    # Stage is irreversible once completed.
    if stored_stage == "completed":
        return "completed"

    # In payment stage: check for confirmation words first.
    if stored_stage == "payment":
        msg_lower = latest_message.lower()
        if any(word in msg_lower for word in PAYMENT_CONFIRMATION_WORDS):
            return "completed"
        # Stay in payment unless customer explicitly changes stage (never go back).
        return "payment"

    # In awaiting_final_confirmation: only yes/no/change advance the state.
    # Everything else keeps the customer in this stage to re-confirm.
    if stored_stage == "awaiting_final_confirmation":
        msg_lower = latest_message.lower().strip()
        words = set(msg_lower.split())
        if words & _CONFIRMATION_YES:
            return "completed"
        if words & _CONFIRMATION_NO:
            # Slot resets happen in webhook.py after stage detection.
            return "order_collection"
        # Customer said something else (off-topic, question) — stay put.
        return "awaiting_final_confirmation"

    # ── Mode A → Mode B transition ────────────────────────────────────────────
    # Only cross into order_collection when a product was already shown
    # (pending_product_sku set) AND the customer explicitly affirms purchase.
    if pending_product_sku and stored_stage in _BROWSING_STAGES:
        msg_lower_stripped = latest_message.lower().strip()
        words_set = set(msg_lower_stripped.split())

        _offered = _last_ai_offered_order(conversation_history)

        # Affirmation after order offer → enter order_collection
        if _offered and (words_set & _PURCHASE_AFFIRMATION):
            return "order_collection"

        # Decline after order offer → stay in browsing (not objection_handling)
        if _offered and (words_set & _DECLINE_ORDER):
            return "product_inquiry"

        # Explicit quantity + product intent (e.g. "2 chahiye", "3 pieces lena")
        _has_qty = bool(re.search(r'\b([1-9]\d?)\b', msg_lower_stripped))
        _has_qty_kw = any(kw in msg_lower_stripped for kw in (
            "piece", "chahiye", "want", "lena", "order", "chahte",
            "dedo", "bhejo", "lenge", "joiye",
        ))
        if _has_qty and _has_qty_kw:
            return "order_collection"

    if _is_order_followup(conversation_history):
        return "order_collection"

    msg = latest_message.lower()

    # Keyword-based stage detection — no AI call, zero latency.
    _ORDER_KW = [
        "order", "buy", "purchase", "confirm", "book", "address", "payment",
        "pay", "upi", "cod", "deliver", "delivery", "chahiye", "lena hai",
        "kharidna", "bhejo", "pakka", "le loon", "abhi lena",
    ]
    _OBJECTION_KW = [
        "sochna", "mehnga", "expensive", "costly", "budget", "nahi", "nope",
        "no thanks", "later", "kal", "baad mein", "not now", "abhi nahi",
        "nhi", "think",
    ]
    _PAYMENT_KW = [
        "upi", "gpay", "phonepe", "paytm", "qr", "payment done", "paid",
        "transferred", "bhej diya", "pay kar diya",
    ]
    _OFF_TOPIC_KW = [
        "cricket", "weather", "politics", "news", "joke", "recipe",
        "movie", "song", "ipl", "modi", "bjp", "congress", "election",
        "doctor", "hospital", "school", "covid",
    ]
    _QUALIFICATION_KW = [
        "how many", "kitne", "quantity", "bulk", "wholesale", "reseller",
        "timeline", "when", "kab", "spec", "size", "colour", "color",
    ]

    if any(kw in msg for kw in _PAYMENT_KW):
        return "payment"
    if any(kw in msg for kw in _ORDER_KW):
        return "offer_making"
    if any(kw in msg for kw in _OBJECTION_KW):
        return "objection_handling"
    if any(kw in msg for kw in _QUALIFICATION_KW):
        return "qualification"
    if any(kw in msg for kw in _OFF_TOPIC_KW):
        return "off_topic"

    # No history → greeting; otherwise assume product inquiry
    if not conversation_history:
        return "greeting"
    return "product_inquiry"


# Section 2 — deterministic multi-slot capture helpers. These let a single
# message like "red and XL size, 2 pieces" fill color+size+quantity in one
# turn, each validated against the pinned SKU's real DB variants, instead of
# re-asking for a value the customer already gave.
_SIZE_ALIASES: dict[str, str] = {
    "extra large": "XL", "extra small": "XS", "x large": "XL", "x small": "XS",
    "small": "S", "medium": "M", "large": "L",
    "xl": "XL", "xxl": "XXL", "xs": "XS", "s": "S", "m": "M", "l": "L",
}


def _extract_size_token(text: str, available_sizes: list[str]) -> str | None:
    """
    Scan text for a size, matching both exact catalogue tokens and common
    aliases ("extra large" -> "XL"), validated against available_sizes only —
    never returns a size the pinned SKU doesn't actually stock.
    """
    size_map = {s.lower(): s for s in available_sizes}
    text_lower = text.lower()
    # Multi-word aliases first ("extra large") so they aren't shadowed by a
    # single-word token match later in the same text.
    for alias, canonical in _SIZE_ALIASES.items():
        if " " in alias and alias in text_lower and canonical in size_map.values():
            return size_map.get(canonical.lower())
    for token in text.split():
        bare = token.lower().strip(".,!?")
        canonical = size_map.get(bare) or size_map.get(_SIZE_ALIASES.get(bare, ""))
        if canonical:
            return canonical
    return None


def _maybe_capture_quantity(conversation, text: str, available_stock: int | None) -> None:
    """
    Scan text for a quantity digit and fill it in-memory if not already set
    and the value is within available_stock. Used so "red XL 2 pieces" fills
    quantity alongside color/size in the same turn instead of re-asking.
    """
    if getattr(conversation, "pending_order_quantity", None):
        return
    match = re.search(r"\b(\d{1,4})\b", text)
    if not match:
        return
    qty = int(match.group(1))
    if qty < 1:
        return
    if available_stock is not None and qty > available_stock:
        return
    conversation.pending_order_quantity = qty
    logger.info("Multi-slot: quantity=%d captured alongside another slot in one message", qty)


def extract_order_field(
    conversation,
    user_text: str,
    variant_info: dict | None = None,
    conversation_history: list[dict] | None = None,
    available_stock: int | None = None,
    saved_address: str | None = None,
) -> tuple[str, object] | None:
    """
    Strict slot-filling extractor: determines which slot we're currently
    collecting via get_next_required_slot(), then runs ONLY the extraction
    logic relevant to that slot.

    Returns one of:
      - (field_name, value)         — valid value for the current slot
      - ("quantity_invalid", stock) — customer requested more than available_stock;
                                      caller should re-prompt without advancing
      - None                        — extraction failed; caller should re-prompt

    Args:
        conversation:         Conversation ORM instance.
        user_text:            The customer's latest message text.
        variant_info:         Dict from catalogue_service.get_product_variant_info.
        conversation_history: List of {'role', 'content'} dicts, most recent last.
        available_stock:      Units available for the pinned product/variant.
                              Used for quantity validation only.
        saved_address:        Previously saved delivery address from customer profile.
                              When set and the agent just offered this address for
                              confirmation, affirmative replies fill it directly.

    Returns:
        Tuple or None as described above.
    """
    text = user_text.strip()
    if not text:
        return None

    vi = variant_info or {}
    next_slot = get_next_required_slot(conversation, vi)

    # All slots already filled — nothing to extract.
    if next_slot is None:
        return None

    text_lower = text.lower()
    words = text.split()

    # ── QUANTITY ──────────────────────────────────────────────────────────────
    if next_slot == "quantity":
        digit_match = re.search(r"\b(\d{1,4})\b", text)
        if not digit_match:
            # No numeric digit found — don't assume 1; re-ask the slot.
            return None
        qty = int(digit_match.group(1))
        # qty < 1 is returned as-is (not clamped to 1) so the caller's
        # write-validation (webhook.py: value < 1 → reject + re-ask) catches
        # it instead of a "0" being silently smuggled in as a valid "1".
        if available_stock is not None and qty > available_stock:
            return ("quantity_invalid", available_stock)
        return ("pending_order_quantity", qty)

    # ── COLOUR ────────────────────────────────────────────────────────────────
    if next_slot == "color":
        for color in vi.get("available_colors", []):
            if color.lower() in text_lower:
                # Section 2 multi-slot: scan the SAME message for size and
                # quantity too ("red and XL size, 2 pieces") so they aren't
                # dropped and the customer isn't re-asked for a value already
                # given — apply directly to the in-memory conversation object;
                # it persists with the next commit in this request, same as
                # any other ORM mutation.
                if vi.get("needs_size"):
                    canonical_size = _extract_size_token(text, vi.get("available_sizes", []))
                    if canonical_size and not getattr(conversation, "selected_size", None):
                        conversation.selected_size = canonical_size
                        logger.info(
                            "Multi-slot: size=%r captured alongside color=%r in one message",
                            canonical_size, color,
                        )
                _maybe_capture_quantity(conversation, text, available_stock)
                return ("selected_color", color)
        return None

    # ── SIZE ──────────────────────────────────────────────────────────────────
    if next_slot == "size":
        # Fix B: if the user types a valid color name while being asked for size,
        # treat it as a color switch — overwrite selected_color so the next slot
        # re-ask uses the new color's in-stock sizes (Fix A).
        for color in vi.get("available_colors", []):
            if re.search(r"\b" + re.escape(color.lower()) + r"\b", text_lower):
                return ("selected_color", color)
        size_map = {s.lower(): s for s in vi.get("available_sizes", [])}
        for token in words:
            canonical = size_map.get(token.lower())
            if canonical:
                _maybe_capture_quantity(conversation, text, available_stock)
                return ("selected_size", canonical)
        return None

    # ── MATERIAL ─────────────────────────────────────────────────────────────
    if next_slot == "material":
        for mat in vi.get("available_materials", []):
            if mat.lower() in text_lower:
                return ("selected_material", mat)
        return None

    # ── CUSTOMER NAME ─────────────────────────────────────────────────────────
    # If the agent asked for name+address together and customer replied with a
    # comma-separated "Name, Address" string, split and capture name now;
    # the address half will be captured when delivery_address becomes next_slot.
    if next_slot == "customer_name":
        history = conversation_history or []
        if _last_agent_asked_name_and_address(history):
            comma_idx = text.find(",")
            if comma_idx > 0:
                name_part = text[:comma_idx].strip()
                if is_valid_name(name_part):
                    return ("customer_name", name_part.title())

        # Strip filler prefixes ("sure, I am", "my name is", "mera naam hai" etc.)
        # before validating — "sure, I am Amit" → candidate "Amit".
        candidate = _strip_name_prefixes(text)
        candidate_words = candidate.split()
        def _is_namelike_word(w: str) -> bool:
            bare = w.replace(".", "")
            return bool(bare) and all(_ud.category(c)[0] in ("L", "M") for c in bare)

        looks_like_name = (
            1 <= len(candidate_words) <= 4
            and all(_is_namelike_word(w) for w in candidate_words)
            and is_valid_name(candidate)
        )
        if looks_like_name:
            return ("customer_name", candidate.title())
        return None

    # ── DELIVERY ADDRESS ─────────────────────────────────────────────────────
    if next_slot == "delivery_address":
        history = conversation_history or []
        # If agent offered a saved address ("Deliver to X? yes/change"):
        #   • affirmation  → write saved address (write-on-confirm fix)
        #   • negation     → return None; next prompt will ask for new address
        #   • anything else (free-text address) → fall through to length check
        if saved_address and _last_agent_offered_saved_address(history):
            _classification = classify_address_confirmation(text)
            if _classification == "confirm":
                return ("delivery_address", saved_address)
            # "change" or "unclear" — neither is a valid address. Return None
            # so the caller re-prompts (asks for a new address, or re-shows
            # the yes/change prompt) instead of falling through to
            # is_valid_address() below.
            return None
        # Customer explicitly asks to reuse the address on file, even when the
        # agent didn't just offer it (e.g. mid-rejection-loop: "use my old address").
        if saved_address and _REUSE_OLD_ADDRESS_RE.search(text):
            return ("delivery_address", saved_address)
        # If last agent message asked for name+address and name wasn't extracted
        # yet (customer skipped the name turn), split the reply.
        if _last_agent_asked_name_and_address(history) and not conversation.customer_name:
            comma_idx = text.find(",")
            if comma_idx > 0:
                name_part = text[:comma_idx].strip()
                if is_valid_name(name_part):
                    # Return name first; address will be captured next turn.
                    return ("customer_name", name_part.title())
        # Guard: a bare SKU token (e.g. "SR27754") is not an address.
        if _SKU_ONLY_PATTERN.match(text.strip()):
            return None
        if is_valid_address(text):
            return ("delivery_address", clean_address(text))
        return None

    # ── MOBILE NUMBER ─────────────────────────────────────────────────────────
    # Only ever reached on channels where it wasn't already auto-filled
    # (Instagram) — WhatsApp pre-fills this slot from the sender's number
    # before slot-filling starts, so get_next_required_slot never lands here.
    if next_slot == "mobile_number":
        if is_valid_mobile_number(text):
            return ("mobile_number", normalize_mobile_number(text))
        return None

    # ── PAYMENT METHOD ────────────────────────────────────────────────────────
    if next_slot == "payment_method":
        upper = text.upper()
        if "COD" in upper or "CASH" in upper:
            return ("payment_method", "COD")
        if any(kw in upper for kw in ("UPI", "GPAY", "PAYTM", "PHONEPE", "PHONEPAY")):
            return ("payment_method", "UPI")
        # Confirm-style slot: when the agent offered a single implied payment method
        # (UPI-only prompt), an affirmative reply fills UPI — same pattern as
        # saved-address confirmation.
        history = conversation_history or []
        if text_lower.strip() in _SLOT_AFFIRMATIVES:
            if _last_agent_offered_upi_only(history):
                return ("payment_method", "UPI")
            # Both UPI + COD were offered and customer said "yes" — ambiguous.
            # Return None so the agent re-asks with explicit options.
        return None

    return None


def get_next_slot_prompt_instruction(
    next_slot: str | None,
    variant_info: dict,
    product,
    available_stock: int | None,
    language: str = "english",
) -> str:
    """
    Return a short, laser-focused instruction for the AI for the current slot.

    This string is injected as a ``current_instruction`` override in the system
    prompt, replacing the general stage instructions when in order_collection or
    awaiting_final_confirmation.  Each instruction asks for EXACTLY ONE piece of
    information so the AI cannot wander.

    Args:
        next_slot:       Current unfilled slot name, "quantity_invalid", or None
                         (all slots filled → show summary).
        variant_info:    Dict from catalogue_service.get_product_variant_info.
        product:         Pinned Product ORM instance (may be None).
        available_stock: Available units for the selected variant/product.
        language:        Customer's detected language.

    Returns:
        Instruction string for the system prompt current_instruction field.
    """
    vi = variant_info or {}
    product_name = getattr(product, "name", "the product") or "the product"
    unit_price = getattr(product, "price", 0) or 0

    if next_slot == "quantity_invalid":
        stock_str = str(available_stock) if available_stock is not None else "limited"
        return (
            f"⚠️ QUANTITY EXCEEDS STOCK — MANDATORY ACTION:\n"
            f"Tell the customer that only {stock_str} piece(s) of {product_name} are available.\n"
            f"Ask them to choose a quantity within that limit.\n"
            f"Do NOT proceed to any other question. Do NOT confirm the order.\n"
            f"Example (English): 'Sorry, only {stock_str} pieces available. How many would you like (1–{stock_str})?'\n"
            f"Example (Hindi): 'Maafi, sirf {stock_str} pieces available hain. Aap kitne lenge (1–{stock_str})?'"
        )

    if next_slot == "quantity":
        return (
            "CRITICAL — IGNORE CONVERSATION FLOW: The quantity has NOT been recorded yet.\n"
            "You MUST ask ONLY: 'How many pieces would you like?' (or Hindi/Gujarati equivalent).\n"
            "Do NOT mention color, size, name, address, or payment.\n"
            "Do NOT confirm the order or show a summary. Do NOT assume quantity = 1.\n"
            "ONE question only."
        )

    if next_slot == "color":
        colors = vi.get("available_colors", [])
        color_list = ", ".join(colors) if colors else "see catalogue"
        return (
            f"CURRENT SLOT: COLOR\n"
            f"Ask ONLY which color the customer wants.\n"
            f"Available colors: {color_list}\n"
            f"Do NOT ask about size, name, address, or payment yet.\n"
            f"Example (English): 'Which color would you like? Available: {color_list}'\n"
            f"NEVER suggest colors not in this list."
        )

    if next_slot == "size":
        sizes = vi.get("available_sizes", [])
        size_list = ", ".join(sizes) if sizes else "see catalogue"
        return (
            f"CURRENT SLOT: SIZE\n"
            f"Ask ONLY which size the customer wants.\n"
            f"Available sizes: {size_list}\n"
            f"Do NOT ask about name, address, or payment yet.\n"
            f"Example (English): 'Which size? Available: {size_list}'\n"
            f"NEVER suggest sizes not in this list."
        )

    if next_slot == "material":
        materials = vi.get("available_materials", [])
        mat_list = ", ".join(materials) if materials else "see catalogue"
        return (
            f"CURRENT SLOT: MATERIAL\n"
            f"Ask ONLY which material the customer wants.\n"
            f"Available materials: {mat_list}\n"
            f"Do NOT ask about name, address, or payment yet."
        )

    if next_slot == "customer_name":
        return (
            "CRITICAL — IGNORE CONVERSATION FLOW: The customer's name has NOT been recorded yet.\n"
            "You MUST ask for their name. Do NOT mention payment, UPI, order summary, confirmation, "
            "or order status. Do NOT invent or assume a name from the conversation.\n"
            "Ask ONLY: 'May I have your name please?' (or Hindi/Gujarati equivalent).\n"
            "Example (English): 'May I have your name please?'\n"
            "Example (Hindi): 'Aapka naam kya hai?'\n"
            "Example (Gujarati): 'Tamaru naam shu chhe?'"
        )

    if next_slot == "delivery_address":
        return (
            "CRITICAL — IGNORE CONVERSATION FLOW: The delivery address has NOT been recorded yet.\n"
            "You MUST ask for the delivery address. Do NOT mention payment, UPI, confirmation, "
            "or order status.\n"
            "Ask ONLY: 'What is your delivery address?' (or Hindi/Gujarati equivalent).\n"
            "Example (English): 'What is your delivery address?'\n"
            "Example (Hindi): 'Delivery address kya hai?'\n"
            "Example (Gujarati): 'Delivery address shu chhe?'"
        )

    if next_slot == "mobile_number":
        return (
            "CRITICAL — IGNORE CONVERSATION FLOW: A delivery contact number has NOT been recorded yet.\n"
            "You MUST ask for a 10-digit mobile number. Do NOT mention payment, UPI, confirmation, "
            "or order status.\n"
            "Ask ONLY: 'What is your mobile number for delivery?' (or Hindi/Gujarati equivalent).\n"
            "Example (English): 'What is your mobile number for delivery?'\n"
            "Example (Hindi): 'Delivery ke liye mobile number kya hai?'\n"
            "Example (Gujarati): 'Delivery mate mobile number shu chhe?'"
        )

    if next_slot == "payment_method":
        return (
            "CURRENT SLOT: PAYMENT METHOD\n"
            "Ask ONLY how the customer would like to pay (UPI or COD if accepted).\n"
            "Do NOT show the order summary yet.\n"
            "Example (English): 'How would you like to pay — UPI or Cash on Delivery?'\n"
            "Example (Hindi): 'Aap UPI se denge ya COD?'"
        )

    # next_slot is None → all slots filled, show summary and ask for confirmation.
    qty = getattr(product, "_order_qty", 0) if product else 0
    total = (unit_price * qty) if (unit_price and qty) else 0
    total_str = f"₹{int(total):,}" if total > 0 else "₹(qty × price)"

    color_line = ""
    size_line = ""
    material_line = ""
    if vi.get("needs_color"):
        color_line = "\n  🎨 Color: [use selected_color from order context]"
    if vi.get("needs_size"):
        size_line = "\n  📐 Size: [use selected_size from order context]"
    if vi.get("needs_material"):
        material_line = "\n  🧵 Material: [use selected_material from order context]"

    return (
        "ALL SLOTS COLLECTED — SHOW FINAL ORDER SUMMARY:\n"
        "Display the complete order summary using EXACT values from the CURRENT ORDER block above.\n"
        f"Include: product name, {color_line}{size_line}{material_line} quantity, "
        f"total price (quantity × unit price), customer name, delivery address, payment method.\n"
        "Then ask: 'Confirm? (yes/no)' — in the customer's language.\n"
        "Do NOT say the order is placed yet. Wait for explicit yes.\n"
        "Format example:\n"
        "  ✅ Order Summary:\n"
        "  ━━━━━━━━━━━━━━━━\n"
        "  📦 [Product] × [Qty] = ₹[Total]\n"
        "  👤 [Name]\n"
        "  📍 [Address]\n"
        "  💳 [Payment]\n"
        "  ━━━━━━━━━━━━━━━━\n"
        "  Confirm? (yes/no)"
    )


# ── Slot-filling state machine ────────────────────────────────────────────────

# Maps logical slot name → conversation ORM column name.
_SLOT_TO_FIELD: dict[str, str] = {
    "quantity": "pending_order_quantity",
    "color": "selected_color",
    "size": "selected_size",
    "material": "selected_material",
    "customer_name": "customer_name",
    "delivery_address": "delivery_address",
    "mobile_number": "mobile_number",
    "payment_method": "payment_method",
}


def get_order_slots(variant_info: dict) -> list[str]:
    """
    Return the ordered list of slots that must be filled for this product.

    For variant products (needs_color / needs_size / needs_material): variant
    attributes come FIRST so quantity is validated against the correct variant
    stock, not meaningless product-level stock.

    Order: (color?) → (size?) → (material?) → quantity → customer_name
    → delivery_address → mobile_number → payment_method.  Non-variant products
    keep the simpler: quantity → customer_name → delivery_address →
    mobile_number → payment_method.

    mobile_number is channel-conditional in practice: on WhatsApp it is
    auto-filled from the sender's number before slot-filling begins (see
    order_pipeline.run_slot_state_machine), so this loop skips straight past
    it; on Instagram it starts empty and is collected like any other slot,
    since the IGSID is not a phone number.

    Args:
        variant_info: Dict returned by catalogue_service.get_product_variant_info.

    Returns:
        Ordered list of slot name strings.
    """
    has_variants = (
        variant_info.get("needs_color")
        or variant_info.get("needs_size")
        or variant_info.get("needs_material")
    )
    slots: list[str] = []
    if has_variants:
        if variant_info.get("needs_color"):
            slots.append("color")
        if variant_info.get("needs_size"):
            slots.append("size")
        if variant_info.get("needs_material"):
            slots.append("material")
    slots.append("quantity")
    slots += ["customer_name", "delivery_address", "mobile_number", "payment_method"]
    return slots


def get_next_required_slot(conversation, variant_info: dict) -> str | None:
    """
    Return the first unfilled slot for the current order, or None if all filled.

    Iterates get_order_slots() in order and returns the first slot whose
    corresponding conversation column is None/empty.  None means all slots are
    collected and the flow is ready for the final confirmation summary.

    Args:
        conversation: Conversation ORM instance.
        variant_info: Dict returned by catalogue_service.get_product_variant_info.

    Returns:
        Slot name string, or None when everything is collected.
    """
    for slot in get_order_slots(variant_info):
        field = _SLOT_TO_FIELD[slot]
        val = getattr(conversation, field, None)
        # quantity is an int — None or 0 both count as unfilled.
        if val is None or val == "":
            return slot
        if slot == "quantity" and val == 0:
            return slot
    return None


def _build_collected_note(collected: dict | None) -> str:
    """
    Build a short note summarising which order_collection fields are already
    saved on the conversation, so the agent does not re-ask them.

    Args:
        collected: Dict with optional keys 'quantity', 'color', 'size',
                   'name', 'address'.

    Returns:
        Multi-line note string, or "" if nothing has been collected yet.
    """
    if not collected:
        return ""
    lines = []
    if collected.get("quantity") is not None:
        lines.append(f"- Quantity: {collected['quantity']} pieces ✅ (already given — do NOT ask again)")
    else:
        lines.append(
            "⚠️  QUANTITY: NOT YET COLLECTED — your VERY NEXT question MUST be "
            "'How many pieces would you like?' NEVER assume, guess, or infer a number. "
            "quantity_collected is ONLY TRUE when the customer explicitly says a number in their message."
        )
    if collected.get("color"):
        lines.append(f"- Color: {collected['color']} ✅ (already given — do NOT ask again)")
    if collected.get("size"):
        lines.append(f"- Size: {collected['size']} ✅ (already given — do NOT ask again)")
    if collected.get("name"):
        lines.append(f"- Name: {collected['name']} ✅ (already given — do NOT ask again)")
    if collected.get("address"):
        lines.append(f"- Address: {collected['address']} ✅ (already given — do NOT ask again)")
    return (
        "\nORDER COLLECTION STATUS (read before asking anything):\n" + "\n".join(lines) + "\n"
    )


def get_stage_instructions(
    stage: str,
    business_type: str,
    products: list,
    collected: dict | None = None,
    accepts_cod: bool = False,
    upi_id: str | None = None,
    upi_display_name: str | None = None,
    cod_limit: int | None = None,
    accepts_upi: bool = True,
    accepts_bank_transfer: bool = False,
    bank_account_name: str | None = None,
    bank_account_number: str | None = None,
    bank_ifsc: str | None = None,
    payment_instructions: str | None = None,
    order_total: float = 0,
    order_product_name: str = "",
    order_qty: int = 0,
    variant_info: dict | None = None,
) -> str:
    """
    Return focused, stage-specific instructions to embed in the system prompt.

    Args:
        stage:              Current conversation stage key.
        business_type:      Client's business type (e.g. 'textile').
        products:           List of product dicts for context.
        collected:          Optional dict with 'quantity', 'name', 'address' keys.
        accepts_cod:        Whether this client accepts Cash on Delivery.
        upi_id:             Client's UPI handle.
        upi_display_name:   Name shown on UPI apps next to UPI ID.
        cod_limit:          Max order value eligible for COD (None = no limit).
        accepts_upi:        Whether UPI is enabled.
        accepts_bank_transfer: Whether bank transfer is enabled.
        bank_account_name:  Bank account holder name.
        bank_account_number: Bank account number.
        bank_ifsc:          Bank IFSC code.
        payment_instructions: Free-text shown after payment details.

    Returns:
        Multi-line instruction string.
    """
    if stage == "greeting":
        return """GREETING STAGE INSTRUCTIONS:
- Give warm welcome with business name
- Ask ONE open question: "What are you looking for today?" (or Hindi/Gujarati equivalent)
- DO NOT ask for the customer's name — name is only collected during order placement
- DO NOT list all products immediately
- DO NOT mention stock counts or quantities
- Build rapport first
- Example (Hindi): "Namaste! 🙏 Aaj kya dekhna chahenge?"
- Example (English): "Welcome! What are you looking for today?"
- If customer has no specific product in mind, offer to share the catalogue link"""

    if stage == "product_inquiry":
        return """PRODUCT INQUIRY STAGE INSTRUCTIONS:
- Show MAX 2-3 most relevant products
- For each product mention:
  → Name + key feature
  → Price (clearly with ₹)
  → Available colors and sizes (from the catalogue above) — list them as options
- ⚠️ STOCK PRIVACY RULE: When first showing a product, do NOT mention stock/piece counts.
  NEVER say "Only X pieces left", "X pieces available", "stock mein X hai", or any number + pieces.
  Only mention available colors/sizes/materials as options.
  If stock is genuinely limited, say "limited stock" or "limited availability" — never a number.
  EXCEPTION: If the customer explicitly asks "how many available", "stock hai kya", "kitne piece
  available hain" — answer TRUTHFULLY with the exact number from catalogue data.
- End with ONE closing question: "Would you like to place an order?" (or language equivalent)
- NEVER ask about color, size, or quantity here.
  Color/size/quantity are collected in order_collection ONLY.
  Mentioning them here confuses the sequence.
- NEVER dump entire catalogue
- If customer just said NO to an order offer: acknowledge warmly ("No problem!"), then ask
  if they'd like to see other items or browse the full catalogue. Do NOT push the same product.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD STOP — FORBIDDEN IN THIS STAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ NEVER ask for customer name or delivery address — those are collected in order_collection ONLY
✗ NEVER say "order placed", "order confirmed", "order ho gaya", "your order has been placed"
✗ NEVER show an order summary or calculate a total to confirm
✗ NEVER share UPI ID, payment link, QR code, or say "please pay ₹X"
✗ NEVER mention COD or cash on delivery as an instruction
✗ NEVER ask for quantity — that is the FIRST question in order_collection
Your ONLY allowed next step toward purchase: ask "Would you like to order?" (one question).
The system will automatically enter order collection when the customer says yes."""

    if stage == "qualification":
        return """QUALIFICATION STAGE INSTRUCTIONS:
- Ask ONE qualifying question at a time:
  → Timeline: "Kab tak chahiye?"
  → Purpose: "Wedding ke liye hai ya daily wear?"
- Use answers to recommend a specific product
- Confirm budget indirectly only if price hesitation detected

HANDLING "yes" / "haan" / "okay" RESPONSES:
If customer replies with only "yes", "haan", "okay", or similar short agreement:
  → Confirm EXACTLY what product they agreed to, then ask "Would you like to order?"
  → NEVER assume and jump straight to asking name/address or quantity.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD STOP — FORBIDDEN IN THIS STAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ NEVER ask for customer name or delivery address
✗ NEVER say "order placed", "order confirmed", "order ho gaya"
✗ NEVER show an order summary or calculate a total to confirm
✗ NEVER share UPI ID, payment link, QR code, or say "please pay ₹X"
✗ NEVER mention COD or cash on delivery as a payment instruction
✗ NEVER ask for quantity — quantity is collected in order_collection ONLY
Your ONLY allowed next step toward purchase: ask "Would you like to order?" (one question)."""

    if stage == "objection_handling":
        return """OBJECTION HANDLING INSTRUCTIONS:
CRITICAL: Reply in the SAME LANGUAGE as the customer (see LANGUAGE RULE at top of prompt).

If customer says price is too high:
→ English: "Quality comes with a fair price. We also have more affordable options — shall I show you?"
→ Hindi: "Haan ji, quality ke saath price thoda upar hota hai. Koi aur option dekhein?"

If customer says they'll think about it:
→ English: "Of course! Just to let you know, stock is limited. Shall I hold a piece for you?"
→ Hindi: "Bilkul ji! Ye piece limited stock mein hai. Aaj confirm karein toh hold kar lete hain."

If customer says they'll do it later:
→ English: "No problem! Feel free to browse and come back anytime."
→ Hindi: "Zaroor ji. Jab bhi tayaar hon, hum yahan hain."

If customer says cheaper elsewhere:
→ English: "We understand. Our quality is guaranteed and delivery is fast. Give us a try?"
→ Hindi: "Hum samajhte hain ji. Quality guarantee hai aur delivery fast hai. Ek baar try karein?"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD STOP — FORBIDDEN IN THIS STAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ NEVER ask for customer name or delivery address
✗ NEVER say "order placed", "order confirmed", "order ho gaya"
✗ NEVER show an order summary or calculate a total to confirm
✗ NEVER share UPI ID, payment link, or say "please pay ₹X"
✗ NEVER mention COD as a payment instruction
✗ NEVER ask for quantity
Your ONLY allowed next step toward purchase: ask "Would you like to order?" (one question)."""

    if stage == "offer_making":
        cod_line = (
            '- If customer hesitates on price: "COD bhi available hai — delivery pe payment"\n'
            if accepts_cod
            else "- NEVER mention COD. This business is UPI-only.\n"
        )
        return f"""OFFER MAKING INSTRUCTIONS:
CRITICAL: Reply in the SAME LANGUAGE as the customer (see LANGUAGE RULE at top of prompt).

- Make a CLEAR offer for the PRODUCT — name + price + delivery time only.
  English example: "[Product Name] — ₹[Price]. Delivery in 3-5 days. Would you like to order?"
  Hindi example: "[Product Name] — ₹[Price]. 3-5 din mein delivery. Order karna chahenge?"
- ⚠️ NEVER mention a quantity, color, or size in the offer itself.
  These are all collected in order_collection, never here.
- Create urgency only when true.
- NEVER be pushy — be helpful
- If customer shows buying intent: ask ONLY "Would you like to order?" — one question.
  The system will automatically enter order collection when they say yes.
  Do NOT jump ahead to ask quantity, color, name, address, or payment here.
- If rejected: offer an alternative product

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD STOP — FORBIDDEN IN THIS STAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ NEVER ask for customer name, delivery address, or quantity
✗ NEVER say "order placed", "order confirmed", "order ho gaya"
✗ NEVER show an order summary or total
✗ NEVER share UPI ID, payment link, QR code, or say "please pay ₹X"
✗ NEVER mention COD as a payment instruction
✗ Your ONLY allowed step: ask "Would you like to order?" then STOP."""

    if stage == "order_collection":
        collected_note = _build_collected_note(collected)
        vi = variant_info or {}
        has_variants = vi.get("has_variants", False)
        needs_color = vi.get("needs_color", False)
        needs_size = vi.get("needs_size", False)
        avail_colors = vi.get("available_colors", [])
        avail_sizes = vi.get("available_sizes", [])

        # Build variant steps only when product actually has them
        if has_variants and needs_color:
            color_list = ", ".join(avail_colors) if avail_colors else "see catalogue"
            variant_step_color = f"""
STEP V1 — COLOR (ask this FIRST — before quantity):
  English:  "Which color would you like? Available: {color_list}"
  Hindi:    "Kaunsa color chahiye? Available: {color_list}"
  Gujarati: "Kayo color joiye? Available: {color_list}"
  NEVER suggest colors not in this list: {color_list}
  If customer picks an unavailable color, tell them and repeat the available list.
"""
        else:
            variant_step_color = "  (This product has no color variants — NEVER ask about color)\n"

        if has_variants and needs_size:
            size_list = ", ".join(avail_sizes) if avail_sizes else "see catalogue"
            variant_step_size = f"""
STEP V2 — SIZE (ask this after color, before quantity):
  English:  "Which size? Available: {size_list}"
  Hindi:    "Kaunsa size chahiye? Available: {size_list}"
  Gujarati: "Kayu size joiye? Available: {size_list}"
  NEVER suggest sizes not in this list: {size_list}
  If customer picks an unavailable size, tell them and repeat the available list.
"""
        else:
            variant_step_size = "  (This product has no size variants — NEVER ask about size)\n"

        # Build the sequence description — variants come BEFORE quantity
        if has_variants and needs_color and needs_size:
            sequence_line = "STEP V1 → COLOR → STEP V2 → SIZE → STEP 1 → QUANTITY → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        elif has_variants and needs_color:
            sequence_line = "STEP V1 → COLOR → STEP 1 → QUANTITY → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        elif has_variants and needs_size:
            sequence_line = "STEP V2 → SIZE → STEP 1 → QUANTITY → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        else:
            sequence_line = "STEP 1 → QUANTITY → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"

        # Summary format depends on whether product has variants
        if has_variants:
            color_line = "  🎨 Color: [Color]\n" if needs_color else ""
            size_line = "  📐 Size: [Size]\n" if needs_size else ""
            summary_format = f"""SUMMARY FORMAT (with variants):
  ✅ Order Summary:
  ━━━━━━━━━━━━━━━━
  📦 [Product Name]
{color_line}{size_line}  🔢 Qty: [Qty] = ₹[Amount]
  👤 [Name]
  📍 [Address]
  ━━━━━━━━━━━━━━━━
  Confirm? (yes/haan/ha)"""
        else:
            summary_format = """SUMMARY FORMAT (no variants):
  ✅ Order Summary:
  ━━━━━━━━━━━━━━━━
  📦 [Product Name] × [Qty] = ₹[Amount]
  👤 [Name]
  📍 [Address]
  ━━━━━━━━━━━━━━━━
  Confirm? (yes/haan/ha)"""

        return f"""ORDER COLLECTION INSTRUCTIONS:

⚠️ MANDATORY SEQUENCE — MEMORISE THIS AND NEVER DEVIATE:
  {sequence_line}

VARIANT RULE — READ FIRST:
  has_variants = {has_variants}
  needs_color  = {needs_color}   available colors: {avail_colors or 'none'}
  needs_size   = {needs_size}    available sizes:  {avail_sizes or 'none'}
  → If has_variants is False: NEVER ask color or size. Jump straight to quantity.
  → If has_variants is True: ask only the variant questions where needed=True.

Check the ALREADY COLLECTED note below before asking anything.
Only ask the FIRST item in the sequence that is still missing.

{variant_step_color}{variant_step_size}
STEP 1 — QUANTITY (ask this AFTER variant attributes if any, before name/address):
  English:  "How many pieces would you like?"
  Hindi:    "Kitne pieces chahiye?"
  Gujarati: "Etla pieces joiye?"
  NEVER assume quantity = 1. NEVER say "I can offer you 1 piece".
  Quantity is validated against the SELECTED variant's stock, not total stock.

STEP 2 — NAME (ask this after quantity+variants, if name is NOT yet collected):
  English:  "May I have your name please?"
  Hindi:    "Aapka naam kya hai?"
  Gujarati: "Tamaru naam shu chhe?"
  ⚠️ NAME MUST come BEFORE ADDRESS. Never ask address before name.
  ⚠️ NAME VALIDATION — A valid customer name:
    - Is at least 2 characters
    - Is NOT a command word: reset, quit, exit, demo, sales, help, start, stop, test, image
    - Does NOT start with "/"
    - Is NOT purely numeric
    - Is NOT a color, size, or payment keyword
  If the customer types a command word (e.g. "reset") when you asked for their name:
    → Do NOT save it as a name. Ask again: "Sorry, could you share your name please?"

STEP 3 — ADDRESS (ask this if name IS collected but address is NOT):
  English:  "What is your delivery address?"
  Hindi:    "Delivery address kya hai?"
  Gujarati: "Delivery address shu chhe?"

ORDER SUMMARY — show once all required fields are collected:
  ⚠️ CRITICAL: NEVER write ₹[Amount] or ₹[amount] — use the REAL calculated amount.
  The CURRENT ORDER section above shows the exact ₹ total — use that number.

{summary_format}

CONFIRMATION — only after customer says yes/haan/ha to summary.

FORBIDDEN PHRASES — NEVER say any of these:
  ✗ "I can offer you 1 piece" / "ek piece de sakta hoon"
  ✗ Asking two questions in the same message
  ✗ Asking address before name
  ✗ Skipping the order summary before confirmation
  ✗ Saying "our team will call you" or "visit our website"
  ✗ Asking color/size when has_variants is False
  ✗ Mentioning colors/sizes not in the available lists above
{collected_note}
RULES:
- ONE question per message only. NEVER ask two things at once.
- Ask in the EXACT sequence above — do not skip or reorder.

QUANTITY VALIDATION:
- If customer requests more than available stock:
  → Tell them the maximum available and ask if they want that quantity.
  → NEVER confirm an impossible quantity."""

    if stage == "payment":
        amount_display = f"₹{int(order_total):,}" if order_total > 0 else "₹[CHECK ORDER SUMMARY ABOVE FOR AMOUNT]"

        # Determine which payment methods are available for this order
        cod_eligible = (
            accepts_cod
            and (cod_limit is None or order_total <= cod_limit)
        )
        upi_eligible = accepts_upi and bool(upi_id)
        bank_eligible = (
            accepts_bank_transfer
            and bool(bank_account_name and bank_account_number and bank_ifsc)
        )

        # Build the list of available methods
        method_lines = []
        if upi_eligible:
            method_lines.append("💳 UPI (GPay / PhonePe / Paytm)")
        if cod_eligible:
            method_lines.append("🚚 Cash on Delivery")
        if bank_eligible:
            method_lines.append("🏦 Bank Transfer")

        methods_available = "\n".join(f"  - {m}" for m in method_lines) if method_lines else "  (none configured — tell customer to contact us)"

        # UPI details
        upi_name_part = f" ({upi_display_name})" if upi_display_name else ""
        upi_line = f"{upi_id}{upi_name_part}" if upi_id else "(UPI ID not configured)"

        # Bank transfer details
        bank_details = (
            f"Account Name: {bank_account_name}\nAccount Number: {bank_account_number}\nIFSC: {bank_ifsc}"
            if bank_eligible else ""
        )

        # COD limit note
        if accepts_cod and cod_limit and order_total > cod_limit:
            cod_note = f"COD is NOT available for this order (limit is ₹{cod_limit:,}, order is {amount_display}). Customer must pay via UPI or bank transfer."
        elif accepts_cod:
            cod_note = "COD is available — confirm COD order immediately when chosen. No PAID step needed for COD."
        else:
            cod_note = "COD is NOT accepted by this business."

        instructions_note = f"\n{payment_instructions}" if payment_instructions else ""

        # Determine default action: if only one method, skip the choice
        if len(method_lines) == 1:
            if upi_eligible:
                default_action = f"""→ Share UPI details immediately:
  "Please pay {amount_display} via UPI:
  UPI ID: {upi_line}
  Send via GPay, PhonePe, or Paytm.{instructions_note}
  Reply PAID when done. ✅\""""
            elif cod_eligible:
                default_action = f"""→ Confirm COD immediately:
  "Your order will be delivered with Cash on Delivery.
  Please keep {amount_display} ready at the time of delivery.{instructions_note}"
  (No PAID step — move to completed.)"""
            else:
                default_action = f"""→ Share bank transfer details immediately:
  "Please transfer {amount_display}:
  {bank_details}{instructions_note}
  Reply PAID when done. ✅\""""
        else:
            default_action = f"""→ Show payment options and ask customer to choose:
  "Please select your payment method:
  {chr(10).join(method_lines)}
  Reply with your choice."

When customer picks UPI:
  "Please pay {amount_display}:
  UPI ID: {upi_line}
  Send via GPay, PhonePe, or Paytm.{instructions_note}
  Reply PAID when done. ✅"

When customer picks Cash on Delivery:
  "Your order will be delivered with Cash on Delivery.
  Please keep {amount_display} ready at the time of delivery.{instructions_note}"
  (No PAID step — order is confirmed immediately for COD.)

When customer picks Bank Transfer:
  "Please transfer {amount_display}:
  {bank_details}{instructions_note}
  Reply PAID when done. ✅\""""

        return f"""PAYMENT STAGE INSTRUCTIONS:

╔══════════════════════════════════════════════════════════╗
║  AMOUNT TO COLLECT: {amount_display:<40}║
╚══════════════════════════════════════════════════════════╝

CRITICAL: NEVER write ₹[amount] or ₹[Amount] — always use: {amount_display}

AVAILABLE PAYMENT METHODS FOR THIS ORDER:
{methods_available}

{cod_note}

{default_action}

When customer says PAID / done / sent / transferred / ho gaya:
→ Confirm immediately: "Payment received! ✅ Order confirmed. Delivery in 3-5 days. Thank you!"

PENDING PAYMENT RULE:
If customer asks ANYTHING off-topic while payment is pending:
→ Answer in ONE sentence, then redirect to payment.
→ Do NOT show new products until payment is confirmed.

NEVER loop on payment question.
NEVER say "Our team will share payment details shortly" — share them NOW."""

    if stage == "completed":
        return """COMPLETED STAGE INSTRUCTIONS:
ORDER IS ALREADY PLACED AND CONFIRMED.
- Thank customer warmly and confirm delivery in 3-5 BUSINESS DAYS (never say "7 days").
- DO NOT try to sell more products unless customer specifically asks.
- DO NOT push upsell.

If customer asks a question after order confirmation:
  → Answer briefly and remind them their order is confirmed.
  → English: "Your order is confirmed! ✅ Delivery in 3-5 business days. Anything else I can help with?"
  → Hindi/Hinglish: "Aapka order confirm ho gaya! ✅ 3-5 din mein delivery. Koi aur help chahiye?"
  → NEVER say "7 days". Always say "3-5 business days".

If customer asks about delivery status:
  → "Your order is confirmed. We'll update you when dispatched."

If customer asks about a DIFFERENT product after ordering:
  → Acknowledge their confirmed order first, then naturally help with the new inquiry.
  → English example: "Your [confirmed product] order is confirmed ✅ Looking for something else? [New Product] is ₹[price] — want to order?"
  → DO NOT say "You've ordered [product] earlier" in a confusing way. Be natural and helpful.

If customer says goodbye or they don't want anything:
  → Be warm, never pushy
  → Share the catalogue link (available in rule #14 above)"""

    if stage == "off_topic":
        return """OFF TOPIC INSTRUCTIONS:
Customer asked something unrelated to business. Politely redirect:

"Ye toh main nahi bata sakta, lekin [Business] ke baare mein koi bhi sawal ho toh zaroor poochhen! 😊
Kya main aapko koi product dikhaaun?"

Redirect examples:
- General knowledge → redirect to products
- News/politics → redirect to products
- Personal advice → redirect to products
- Competitor info → "Hum sirf apne products ke baare mein baat kar sakte hain"

GOODBYE / NOT INTERESTED HANDLING:
If customer says "no thanks", "not interested", "bye", "goodbye", "later", "theek hai", "nahi chahiye":
  → Be warm and understanding, never pushy
  → Share the catalogue link (see rule #14 in strict rules)
  → Leave the door open for future
  → English example: "No problem! Feel free to browse our collection anytime. We're here whenever you need us 😊"
  → Hinglish example: "Bilkul, koi baat nahi! Jab bhi chahiye, hum yahan hain 😊" """

    return ""
