"""
Intent detection service — classifies customer messages by sales intent.

Two-tier approach:
  1. Clear signals: instant keyword match, no AI needed.
  2. Ambiguous words ("ok", "yes"): returned as "ambiguous" so the caller
     can use conversation stage to decide the correct classification.
  3. Lead scoring: stage-first (more accurate), message-keyword fallback.

All lists use lowercase tokens; matching is case-insensitive substring search.
"""

from __future__ import annotations

CLEAR_ORDER_SIGNALS = [
    "order karna hai", "buy karna hai", "lena hai",
    "book karo", "confirm", "address hai",
    "payment karte hain", "order confirm", "pakka",
    "abhi order", "le loon", "fix kar",
]

CLEAR_REJECTION_SIGNALS = [
    "nahi chahiye", "cancel", "don't want", "not interested",
    "baad mein", "nahi lena", "mat bhejo", "band karo",
    "nope", "nhi chahiye",
]

# Single-word ambiguous replies that depend on conversation context
_AMBIGUOUS_TOKENS = {
    "ok", "okay", "yes", "haan", "sure",
    "theek", "fine", "alright", "ha", "yep",
}

# Legacy signal lists (kept for order_score / confidence compatibility)
ORDER_SIGNALS = [
    "order", "chahiye", "lena", "kharidna",
    "book", "confirm", "delivery", "send",
    "bhejo", "le loon", "fix kar", "pakka",
    "haan", "theek hai", "ok", "done",
    "address", "naam", "payment", "upi",
    "kitne ka", "total", "cod", "cash",
    "ready", "abhi", "jaldi", "kal milega",
]

REJECTION_SIGNALS = [
    "nahi", "no", "mat", "baad mein",
    "sochna", "expensive", "mehnga",
    "budget nahi", "abhi nahi", "kal",
    "nhi", "nope", "cancel", "band karo",
]

OFF_TOPIC_SIGNALS = [
    "cricket", "weather", "politics", "news",
    "joke", "recipe", "help with", "explain",
    "what is", "who is", "kya hota", "batao",
    "movie", "song", "gana", "film", "ipl",
    "modi", "bjp", "congress", "election",
    "covid", "doctor", "hospital", "school",
]

# Signals used by classify_lead()
GENUINE_HOT_SIGNALS = [
    "order karna hai", "buy", "purchase",
    "kitna total", "address", "upi", "payment",
    "confirm", "book", "lena hai confirmed",
    "payment karte hain", "order confirm",
]

BROWSING_SIGNALS = [
    "delivery time", "kitne din", "return policy",
    "material kya hai", "photos", "show me",
    "available hai", "stock", "kitna hai",
    "size", "colour", "color", "variant",
]


def detect_intent_fast(message: str) -> str:
    """
    Return a quick intent classification without an AI call.

    Returns:
        "order"     — clear buying signal
        "rejection" — clear disinterest
        "ambiguous" — single neutral word; caller must use stage context
        "browsing"  — default / exploring
    """
    msg = message.lower().strip()

    for signal in CLEAR_ORDER_SIGNALS:
        if signal in msg:
            return "order"

    for signal in CLEAR_REJECTION_SIGNALS:
        if signal in msg:
            return "rejection"

    if msg in _AMBIGUOUS_TOKENS:
        return "ambiguous"

    return "browsing"


def classify_lead(message: str, stage: str) -> str:
    """
    Return lead heat category: "hot", "warm", or "cold".

    Stage-based classification takes priority because the conversation stage
    already encodes confirmed buying intent more reliably than keyword matching.

    Args:
        message: Latest customer message.
        stage:   Current conversation stage key.

    Returns:
        "hot" / "warm" / "cold"
    """
    if stage in ("order_collection", "payment", "completed"):
        return "hot"
    if stage in ("offer_making", "qualification"):
        return "warm"

    msg = message.lower()

    hot_score = sum(1 for s in GENUINE_HOT_SIGNALS if s in msg)
    if hot_score >= 1:
        return "hot"

    browsing_score = sum(1 for s in BROWSING_SIGNALS if s in msg)
    if browsing_score >= 1:
        return "warm"

    return "cold"


def detect_intent(message: str) -> dict:
    """
    Score a customer message for order intent, rejection, and off-topic content.

    Maintains backward-compatible return shape so existing callers don't break.
    Uses the fast two-tier approach internally.

    Args:
        message: Raw customer message text.

    Returns:
        Dict with keys:
          is_order_intent (bool)  — clear or likely order signal
          is_rejection    (bool)  — clear rejection signal
          is_off_topic    (bool)  — off-topic content
          order_score     (int)   — count of legacy order signals matched
          confidence      (str)   — "high" / "medium" / "low"
          fast_intent     (str)   — result of detect_intent_fast()
    """
    msg = message.lower()

    fast = detect_intent_fast(message)

    order_score = sum(1 for signal in ORDER_SIGNALS if signal in msg)
    rejection_score = sum(1 for signal in REJECTION_SIGNALS if signal in msg)
    off_topic_score = sum(1 for signal in OFF_TOPIC_SIGNALS if signal in msg)

    if order_score >= 2:
        confidence = "high"
    elif order_score == 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "is_order_intent": fast in ("order",) or order_score >= 2,
        "is_rejection": fast == "rejection" or (rejection_score >= 1 and order_score == 0),
        "is_off_topic": off_topic_score >= 1 and order_score == 0,
        "order_score": order_score,
        "confidence": confidence,
        "fast_intent": fast,
    }
