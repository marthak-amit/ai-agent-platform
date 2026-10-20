"""
Irrelevant-message guard — stop spending money on a customer who only sends noise.

A conversation keeps one counter, `Conversation.off_topic_count`: the number of CONSECUTIVE irrelevant messages.
What counts as irrelevant:
  * emoji-only / symbol-only messages and laughter ("😂😂😂", "???", "hahaha", "lol") — detected here, ₹0;
  * messages the router (`out_of_scope`) or the keyword pipeline's off-topic check flags as unrelated to the business.
Acknowledgement emojis (👍 🙏 ❤️ 😊 …) are NOT irrelevant — they are normal thanks — and neither count nor reset.

Escalation (`irrelevant_streak_limit`, default 3, per client via `Client.off_topic_threshold`):
  1. below the limit: a fixed template reply (never model text);
  2. at the limit: ONE final fixed notice, then the conversation is MUTED for `irrelevant_mute_minutes`
     (`Conversation.spam_muted_until`): free text is saved but not answered — no router call, no LLM, no reply;
  3. while muted, ₹0 things still work: SKUs, bare numbers / yes / no / COD / UPI, catalogue pastes, button taps,
     images (payment proof) and "talk to someone". The mute ends by itself, or when staff resume the bot.
An on-topic message resets the counter. Nothing is ever muted while an order is in progress — a name or an address
must still be accepted — so in order stages irrelevant noise is simply not answered.

This module is pure helpers (no pipeline imports); the guard itself runs in `order_pipeline.run_spam_guard`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import get_settings

logger = logging.getLogger("app.services.spam_guard")

#: Stored stages in which an order is being built or paid for (never muted, noise is just ignored).
ORDER_STAGES = frozenset({"order_collection", "awaiting_final_confirmation", "payment", "awaiting_switch_confirm"})

# Thanks / acknowledgement emojis — a normal reply, not noise.
_ACK_EMOJIS = frozenset(
    "👍🙏❤♥💖💕💗💐🌹🌸😊🙂☺😍🥰😘👌✅🤝🙌👏💯🎉🙇😇"
)
# Characters that decorate an emoji without being one: variation selectors, ZWJ, skin-tone modifiers.
_EMOJI_GLUE = frozenset({"️", "‍", "︎"}) | {chr(c) for c in range(0x1F3FB, 0x1F400)}
_LAUGHTER_RE = re.compile(r"^(?:(?:ha){2,}h?|(?:he){2,}h?|(?:hi){3,}|l+o+l+|lmao|lmfao|rofl|xd+|hehe+|haha+)$", re.IGNORECASE)


def _core(text: str) -> str:
    """The text without whitespace and emoji glue."""
    return "".join(ch for ch in (text or "") if not ch.isspace() and ch not in _EMOJI_GLUE)


def classify_text(text: str) -> str:
    """
    'ack'  — only thanks/acknowledgement emojis (👍🙏❤️…): answer as usual, count nothing;
    'junk' — no letters or digits at all (😂😂😂, ???, …, 🤔) or pure laughter ("hahaha", "lol"): irrelevant;
    'text' — anything else.
    """
    core = _core(text)
    if not core:
        return "junk" if (text or "").strip() else "text"
    if all(ch in _ACK_EMOJIS for ch in core):
        return "ack"
    if _LAUGHTER_RE.match(core):
        return "junk"
    if any(unicodedata.category(ch)[0] in ("L", "N") for ch in core):
        return "text"
    return "junk"


def streak_limit(client) -> int:
    """Consecutive irrelevant messages allowed before the final notice + mute (client override, else the setting)."""
    override = getattr(client, "off_topic_threshold", None)
    return max(1, int(override or get_settings().irrelevant_streak_limit))


def mute_minutes() -> int:
    """Length of the mute in minutes (0 = never mute)."""
    return max(0, int(get_settings().irrelevant_mute_minutes))


def _utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive timestamp to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_muted(conv, now: datetime | None = None) -> bool:
    """True while the conversation's mute is in the future."""
    until = _utc(getattr(conv, "spam_muted_until", None))
    return until is not None and (now or datetime.now(timezone.utc)) < until


def mute_expired(conv, now: datetime | None = None) -> bool:
    """True when a mute was set and has run out (the streak must then start from zero)."""
    until = _utc(getattr(conv, "spam_muted_until", None))
    return until is not None and (now or datetime.now(timezone.utc)) >= until


@dataclass(frozen=True)
class Strike:
    """Outcome of registering one irrelevant message."""

    count: int
    limit: int
    reached: bool       # the streak just hit (or passed) the limit
    muted: bool         # a mute was started by this strike


async def _write(db, conv, field: str, value) -> None:
    """Persist one conversation column (late import: conversation_service pulls in the whole pipeline's models)."""
    from app.services import conversation_service

    await conversation_service.update_order_field(db, conv.id, field, value)


async def register_strike(db, conv, client, *, may_mute: bool) -> Strike:
    """
    Count one more consecutive irrelevant message. At the limit, start the mute when `may_mute` (no order in
    progress) and `irrelevant_mute_minutes` > 0. Never raises on a failed write — the counter is a safeguard.
    """
    count = (getattr(conv, "off_topic_count", 0) or 0) + 1
    limit = streak_limit(client)
    reached = count >= limit
    muted = False
    try:
        conv.off_topic_count = count
        await _write(db, conv, "off_topic_count", count)
        minutes = mute_minutes()
        if reached and may_mute and minutes > 0:
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            conv.spam_muted_until = until
            await _write(db, conv, "spam_muted_until", until)
            muted = True
            logger.warning(
                "SPAM mute conv=%s client=%s streak=%d until=%s", getattr(conv, "id", None), getattr(client, "id", None),
                count, until.isoformat(timespec="seconds"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("spam strike write failed conv=%s: %s", getattr(conv, "id", None), exc)
    return Strike(count=count, limit=limit, reached=reached, muted=muted)


async def clear(db, conv) -> None:
    """Back on topic (or the mute ran out): reset the streak and lift any mute. Writes only what is set."""
    try:
        if getattr(conv, "off_topic_count", 0):
            conv.off_topic_count = 0
            await _write(db, conv, "off_topic_count", 0)
        if getattr(conv, "spam_muted_until", None) is not None:
            conv.spam_muted_until = None
            await _write(db, conv, "spam_muted_until", None)
    except Exception as exc:  # noqa: BLE001
        logger.error("spam clear failed conv=%s: %s", getattr(conv, "id", None), exc)
