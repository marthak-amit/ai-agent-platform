"""
Escalation detection and human-handoff service.

Detects three escalation scenarios:
  1. Prompt injection attempts — return a safe canned reply, skip AI.
  2. Angry / complaint triggers — notify owner via WhatsApp, let AI respond warmly.
  3. Repeated unanswered questions — notify owner, let AI respond.

Public API:
  check_escalation_needed(message, history, conversation_id) → dict
  handle_escalation(escalation, client, conversation, customer_phone, db) → str | None
  notify_owner_escalation(client, conversation, reason, db) → None
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all instructions",
    "forget your instructions",
    "forget instructions",
    "you are now",
    "act as",
    "pretend you are",
    "pretend you",
    "system prompt",
    "jailbreak",
    "bypass",
    "override instructions",
    "new instructions",
    "disregard",
]

# Complaint / anger words that always hand the customer to a human (English + Hinglish, matched as whole words).
ESCALATION_TRIGGERS = [
    # Angry / frustrated
    "angry", "frustrated", "useless", "terrible",
    "worst", "scam", "fraud", "cheating",
    "bakwaas", "bekar", "dhoka", "pagal",
    # Complaint / legal
    "complaint", "damaged",
    "wrong product",
    "manager", "owner se baat",
]

# Words that signal a complaint only when stated, not when asked: "what is your return policy?" is an FAQ
# (and used to silence the bot for the whole conversation), "I want a refund" is a complaint.
SOFT_ESCALATION_TRIGGERS = ["refund", "return", "cancel order", "report"]

# The same complaint / fraud / damage / refund words in the other scripts the shop's customers write
# (substring match — these scripts have no ASCII word boundaries). Native-script shoppers used to get no
# handoff at all on this keyword path.
ESCALATION_TRIGGERS_NATIVE = [
    "शिकायत", "धोखा", "धोका", "फ्रॉड", "फर्जी", "खराब", "टूटा", "रिफंड", "बेकार", "घटिया", "मालिक से बात",
    "ફરિયાદ", "ઠગાઈ", "છેતરપિંડી", "ફ્રોડ", "ખરાબ", "તૂટેલ", "રિફંડ", "બકવાસ", "માલિક સાથે વાત",
    "तक्रार", "फसवणूक", "तुटलेला", "परतावा", "मालकाशी बोल",
    "অভিযোগ", "প্রতারণা", "রিফান্ড", "খারাপ", "மோசடி", "புகார்", "பணத்தை திரும்ப", "మోసం", "ఫిర్యాదు", "రీఫండ్",
    "ವಂಚನೆ", "ದೂರು", "ರಿಫಂಡ್", "വഞ്ചന", "പരാതി", "റീഫണ്ട്", "ਧੋਖਾ", "ਸ਼ਿਕਾਇਤ", "ਖਰਾਬ", "ਰਿਫੰਡ", "ଠକେଇ", "ଅଭିଯୋଗ",
    "شکایت", "دھوکہ", "فراڈ", "خراب", "ریفنڈ",
]

_QUESTION_STARTERS = (
    "what", "how", "when", "where", "which", "who", "can", "could", "do", "does", "is", "are", "will", "kya", "kaise",
    "kab", "kitne", "su", "shu", "kem",
)


def _is_question(msg: str) -> bool:
    """True for a question ('?' / '؟' or a leading question word)."""
    stripped = msg.strip()
    first = stripped.split()[0] if stripped.split() else ""
    tokens = set(re.findall(r"[a-z]+", stripped))
    return "?" in stripped or "؟" in stripped or first in _QUESTION_STARTERS or bool(tokens & {"kya", "policy", "kaise", "kab"})


def _has_word(msg: str, word: str) -> bool:
    """Whole-word / whole-phrase match for ASCII triggers (so 'return' does not fire inside 'returning')."""
    return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", msg) is not None


def count_escalation_triggers(msg: str) -> int:
    """Number of complaint triggers in an already lower-cased message (soft ones only count for statements)."""
    hard = sum(1 for t in ESCALATION_TRIGGERS if _has_word(msg, t)) + sum(1 for t in ESCALATION_TRIGGERS_NATIVE if t in msg)
    if hard or _is_question(msg):
        return hard
    return sum(1 for t in SOFT_ESCALATION_TRIGGERS if _has_word(msg, t))


async def check_escalation_needed(
    message: str,
    conversation_history: list[dict],
    conversation_id: int,
) -> dict:
    """
    Determine whether this message requires escalation.

    Checks (in priority order):
      1. Prompt injection patterns → return canned reply, do NOT call AI.
      2. Customer anger / complaint keywords → notify owner, AI still replies.
      3. Customer repeated same question ≥3 times → notify owner, AI still replies.

    Args:
        message:              Latest customer message.
        conversation_history: List of {"role", "content"} dicts (oldest first).
        conversation_id:      PK of the Conversation row (for logging).

    Returns:
        Dict with keys:
          escalate       (bool)
          reason         (str | None)  — "prompt_injection" | "customer_escalation" | "repeated_question"
          notify_owner   (bool)
          response       (str | None)  — pre-built reply for injection; None means AI handles it
    """
    msg = message.lower()

    # ── 1. Prompt injection ────────────────────────────────────────────────────
    for pattern in PROMPT_INJECTION_PATTERNS:
        if _has_word(msg, pattern):
            logger.warning(
                "Prompt injection attempt on conversation %d: %r",
                conversation_id,
                message[:80],
            )
            return {
                "escalate": True,
                "reason": "prompt_injection",
                "notify_owner": False,
                "response": None,   # the caller renders the localised `injection_refusal` template
            }

    # ── 2. Anger / complaint ───────────────────────────────────────────────────
    trigger_count = count_escalation_triggers(msg)
    if trigger_count >= 1:
        return {
            "escalate": True,
            "reason": "customer_escalation",
            "notify_owner": True,
            "response": None,
        }

    # ── 3. Repeated question (3+ times) ───────────────────────────────────────
    if len(conversation_history) >= 6:
        customer_msgs = [
            m["content"]
            for m in conversation_history[-6:]
            if m.get("role") == "user"
        ]
        if len(customer_msgs) >= 3:
            last_words = set(message.lower().split())
            repetition = sum(
                1
                for prev in customer_msgs[:-1]
                if len(last_words & set(prev.lower().split())) > 2
            )
            if repetition >= 2:
                return {
                    "escalate": True,
                    "reason": "repeated_question",
                    "notify_owner": True,
                    "response": None,
                }

    return {"escalate": False, "reason": None, "notify_owner": False, "response": None}


async def handle_escalation(
    escalation: dict,
    client,
    conversation,
    customer_phone: str,
    db,
) -> str | None:
    """
    Act on a detected escalation — notify owner and/or return a safe canned reply.

    Args:
        escalation:    Result dict from check_escalation_needed().
        client:        Client ORM instance.
        conversation:  Conversation ORM instance.
        customer_phone: Customer's phone/IGSID.
        db:            Active async DB session.

    Returns:
        Safe reply string if AI should be bypassed (prompt injection).
        None if AI should still respond (owner has been notified).
    """
    reason = escalation.get("reason")

    # Record escalation on the conversation row (best-effort)
    try:
        from sqlalchemy import select as _select
        from app.models.conversation import Conversation as _Conv
        from sqlalchemy.ext.asyncio import AsyncSession as _AS
        if isinstance(db, _AS):
            conv_result = await db.execute(
                _select(_Conv).where(_Conv.id == conversation.id).limit(1)
            )
            conv_row = conv_result.scalar_one_or_none()
            if conv_row and hasattr(conv_row, "escalation_count"):
                conv_row.escalation_count = (conv_row.escalation_count or 0) + 1
                conv_row.last_escalation_at = datetime.now(timezone.utc)
                await db.commit()
    except Exception as exc:
        logger.debug("Could not increment escalation_count: %s", exc)

    # Prompt injection — skip AI entirely, return safe canned reply
    if reason == "prompt_injection":
        logger.warning("Prompt injection from %s", customer_phone)
        business = getattr(client, "business_name", "this business") if client else "this business"
        from app.services.language_templates import get_template

        return get_template(
            getattr(conversation, "last_customer_language", None) or "english", "injection_refusal", business=business,
        )

    # Customer upset / repeated question — notify owner, let AI respond
    if escalation.get("notify_owner"):
        await notify_owner_escalation(client, conversation, reason or "unknown", db)

    return None


async def notify_owner_escalation(
    client,
    conversation,
    reason: str,
    db,
) -> None:
    """
    Send a WhatsApp alert to the business owner when a customer needs attention.

    Args:
        client:       Client ORM instance (must have .phone and .whatsapp_phone_number_id).
        conversation: Conversation ORM instance.
        reason:       Escalation reason key.
        db:           Active async DB session (unused but kept for future DB writes).
    """
    from app.services import outbound

    reason_text = {
        "customer_escalation": "⚠️ Customer seems upset",
        "repeated_question": "⚠️ Customer asked the same thing multiple times",
    }.get(reason, "⚠️ Customer needs attention")

    customer_phone = getattr(conversation, "phone_number", "unknown")

    message = (
        f"{reason_text}\n\n"
        f"Customer: {customer_phone}\n"
        f"Please check and respond manually if needed.\n"
        f"Dashboard: /conversations"
    )

    if not (client and client.phone) or getattr(conversation, "is_sandbox", False):
        return      # (simulator conversations never ping the owner)

    try:
        await outbound.send_owner_text(client.phone, message)
        logger.info(
            "Escalation alert sent to owner %s (reason=%s, customer=%s).",
            client.phone,
            reason,
            customer_phone,
        )
    except Exception as exc:
        logger.error("Failed to send escalation alert: %s", exc)
