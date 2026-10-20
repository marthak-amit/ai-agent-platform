"""
Order-draft recovery, cancel confirmation and the small lexicons that protect a customer from a wrong guess.

Three things a real conversation (conv=60, Riya Sarees) showed the bot getting wrong:

  * "sorry i cant get it!" was read as `cancel_order` and the draft was wiped. A cancel now needs an explicit cancel
    word AND a very confident router verdict; anything else asks "Cancel this order? (Yes / No)" first.
    State: `Conversation.cancel_confirm_at` (the question is open), answered by `run_recovery_guard`.
  * Whatever cancels a draft/order in chat saves a snapshot first (`save_cancelled_draft` ->
    `Conversation.last_cancelled_draft`, valid for `draft_restore_ttl_minutes`), so "I want to continue this" /
    "yes" afterwards restores it instead of getting a generic "Sure, let's continue!".
  * "what are you trying to say" / "samjh nahi aaya" / "?" is `confused`: nothing changes, the open question is
    re-sent in its simplest form (see `router_actions.handle_confused`).

Pure helpers (lexicons, snapshot building, TTL checks) have no database access and are unit-tested directly; the
`run_*` hook at the bottom is the pipeline entry point (it needs the DB and the reply plumbing).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services import command_words as cw

logger = logging.getLogger(__name__)

# Conversation columns that make up an order draft (what a cancel wipes and a restore writes back).
DRAFT_FIELDS: tuple[str, ...] = (
    "pending_product_sku", "pending_order_quantity", "selected_color", "selected_size", "selected_material",
    "customer_name", "delivery_address", "payment_method", "summary_shown",
    "cart_items", "cart_variant_mode", "cart_collection_mode", "cart_wip_item", "cart_pending_confirmation",
)
_ORDER_STAGES = frozenset({"order_collection", "awaiting_final_confirmation", "payment", "awaiting_switch_confirm"})


# ── lexicons ─────────────────────────────────────────────────────────────────

# Phrases that say "I am lost" — never "stop my order".
_CONFUSED_PHRASES: tuple[str, ...] = (
    "what are you trying to say", "what are you saying", "what do you mean", "what you mean", "what does that mean",
    "whats going on", "what is going on", "confusing", "so confusing", "confused", "i am confused", "im confused",
    "makes no sense", "make no sense", "not clear", "unclear", "explain again", "explain properly", "say that again", "say again",
    "samjh nahi aaya", "samajh nahi aaya", "samjh nahi aa raha", "samajh nahi aa raha", "samajh nahi aata", "samjha nahi", "samjhi nahi",
    "samajh nahi", "samjh nahi", "kya bol rahe ho", "kya keh rahe ho", "kya kehna chahte ho", "kya matlab", "matlab kya", "kuch samajh nahi",
    "samajayu nahi", "samjayu nahi", "samajaatu nahi", "shu kevu chhe", "su kevu chhe", "shu kahevu chhe", "su kahevu chhe", "shu kaho chho", "su kaho cho",
    "समझ नहीं आया", "समझ नहीं आ रहा", "समझ नहीं आता", "समझ में नहीं आया", "क्या बोल रहे हो", "क्या कह रहे हो", "क्या कहना चाहते", "क्या मतलब", "मतलब क्या",
    "સમજાયું નહીં", "સમજાતું નથી", "સમજ ન પડી", "સમજ નથી પડી", "શું કહો છો", "શું કહેવા માંગો છો", "શું કેવું છે",
)
# "I can't get it" / "i dont understand" / "not getting it" — only when the sentence ENDS there ("can't get it in blue" is
# a product question, not confusion).
_CONFUSED_RE = re.compile(
    r"\b(?:cant|cannot|can\s+not|couldnt|could\s+not|dont|do\s+not|didnt|did\s+not|wont)\s+(?:get|got|understand|follow)"
    r"(?:\s+(?:it|this|that|you|what\s+you\s+(?:say|mean)|anything))?$|\bnot\s+(?:getting|following)\s+(?:it|this|that)$",
    re.IGNORECASE,
)
_QUESTION_MARKS_ONLY_RE = re.compile(r"^[\s?؟¿❓❔]+$")

# Cancel wording that is unambiguous. Weak words ("stop", "quit", "no") are NOT in here on purpose.
_EXPLICIT_CANCEL_PHRASES: tuple[str, ...] = (
    "cancel", "cancel karo", "cancel kar do", "cancel my order", "cancel the order", "cancel this order", "cancel it",
    "nahi chahiye", "nahin chahiye", "nahi lena", "nahi karna", "rehne do", "rahne do", "rahevane do", "bas rehne do",
    "dont want", "do not want", "i dont want", "i do not want", "stop order", "stop my order", "stop the order", "stop this order",
    "रद्द", "कैंसल", "नहीं चाहिए", "नहीं लेना", "रहने दो", "રદ", "કેન્સલ", "નથી જોઈતું", "નથી લેવું", "રહેવા દો",
)
_NEGATED_CANCEL_RE = re.compile(
    r"\b(?:dont|do\s*not|don\s*t|never|nahi|na|mat|nathi)\s+(?:want\s+to\s+)?cancel"
    r"|\b(?:didnt|did\s*not|wasnt|was\s*not|never)\s+(?:mean|meant|want|wanted|intend|intended)\s+to\s+cancel"
    r"|\bcancel(?:led|ed)?\s+(?:it\s+)?(?:by\s+mistake|wrongly)\b",
    re.IGNORECASE,
)

_RESUME_PHRASES: tuple[str, ...] = (
    "continue", "continue this", "continue my order", "continue with it", "continue karo", "resume", "carry on", "go on", "go ahead",
    "restore", "restore it", "restore my order", "bring it back", "bring back my order", "get it back", "undo", "undo that",
    "i did not mean to cancel", "i didnt mean to cancel", "i didnt want to cancel", "i did not want to cancel", "by mistake",
    "galti se", "galti hui", "mistake se", "wapas lao", "wapas chalu", "chalu karo", "chalu rakho", "jari rakho", "aage badho", "aage chalo",
    "pachu chalu karo", "फिर से शुरू", "वापस लाओ", "जारी रखो", "चालू करो", "ચાલુ રાખો", "પાછું લાવો",
)
_RESUME_YES_EXTRA = frozenset({"ye", "yea", "ya", "yaa", "yess", "yesss", "yup", "haa", "haan", "han", "ha", "ji", "ok", "okay", "sure"})
# Roman-script "yes" of languages command_words only knows in their own script (Marathi "ho"/"hoy").
_ANSWER_YES_EXTRA = frozenset({"ho", "hoy", "hoye", "hun", "jarur", "zaroor"})


def is_yes_answer(text: str) -> bool:
    """A yes to a yes/no question the bot just asked (command_words plus Roman-script Marathi/Hindi variants)."""
    tokens = _norm(text).split()
    return cw.is_yes(text) or (0 < len(tokens) <= 2 and set(tokens) <= (_ANSWER_YES_EXTRA | _RESUME_YES_EXTRA) and not cw.is_no(text))


def _norm(text: str) -> str:
    """Lower-case, punctuation/emoji removed, apostrophes dropped ("can't" -> "cant")."""
    return cw.normalize((text or "").replace("'", "").replace("’", ""))


def _contains_phrase(norm: str, phrases: tuple[str, ...]) -> bool:
    """True when the normalised text contains one of the (already plain) phrases on word boundaries."""
    padded = f" {norm} "
    return any(f" {cw.normalize(p.replace(chr(39), ''))} " in padded for p in phrases)


def is_confused(text: str) -> bool:
    """
    The customer says they are lost ("I can't get it", "what are you trying to say", "samjh nahi aaya").

    Only a short message counts (a long one is a real request that happens to contain such a word), and a cancel
    request never does.
    """
    norm = _norm(text)
    if not norm or len(norm.split()) > 12:
        return False
    if has_explicit_cancel_keyword(text):
        return False
    return _contains_phrase(norm, _CONFUSED_PHRASES) or bool(_CONFUSED_RE.search(norm))


def is_question_marks_only(text: str) -> bool:
    """A message that is nothing but '?' / '??' (the customer is asking us to explain)."""
    return bool(text) and "?" in text.replace("؟", "?") and bool(_QUESTION_MARKS_ONLY_RE.match(text))


def has_explicit_cancel_keyword(text: str) -> bool:
    """
    True when the message contains unmistakable cancel wording ("cancel", "nahi chahiye", "rehne do", "don't want",
    "stop order"). A negated cancel ("don't cancel") and a question about cancelling ("can I cancel?") do not count.
    """
    norm = _norm(text)
    if not norm:
        return False
    padded = f" {norm} "
    if _NEGATED_CANCEL_RE.search(norm) or any(f" {p} " in padded for p in ("dont cancel", "do not cancel", "mat cancel")):
        return False
    if set(norm.split()) & cw._STRONG_CANCEL:
        return not any(mark in (text or "") for mark in cw._QUESTION_MARKS)
    return _contains_phrase(norm, _EXPLICIT_CANCEL_PHRASES)


def is_resume_request(text: str) -> bool:
    """"continue" / "yes" / "ye" / "haan" / "I want to continue this" / "restore my order" — wants the cancelled order back."""
    norm = _norm(text)
    tokens = norm.split()
    if not tokens or len(tokens) > 10:
        return False
    if has_explicit_cancel_keyword(text):
        return False
    if cw.is_yes(text) or (len(tokens) <= 2 and set(tokens) <= _RESUME_YES_EXTRA):
        return True
    if _contains_phrase(norm, _RESUME_PHRASES):
        return True                                  # "i didnt mean to cancel" — says cancel, means the opposite
    return cw.matches("continue", text) and not cw.is_no(text)


# ── snapshot of a cancelled draft ────────────────────────────────────────────

def _utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive timestamp to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def draft_ttl() -> timedelta:
    """How long a cancelled draft can be restored."""
    return timedelta(minutes=max(1, int(get_settings().draft_restore_ttl_minutes)))


def build_snapshot(conv, now: datetime | None = None) -> dict | None:
    """
    JSON snapshot of the conversation's order draft, or None when there is no draft (no pinned product).

    `fields` holds every DRAFT_FIELDS column that has a value; `ts` is when the snapshot was taken.
    """
    if not getattr(conv, "pending_product_sku", None):
        return None
    fields = {}
    for name in DRAFT_FIELDS:
        value = getattr(conv, name, None)
        if value not in (None, "", [], {}) and not (name == "summary_shown" and value is False):
            fields[name] = value
    return {"ts": (now or datetime.now(timezone.utc)).isoformat(), "fields": fields}


def restorable_draft(conv, now: datetime | None = None) -> dict | None:
    """The saved snapshot while it is still within the restore window, else None."""
    snap = getattr(conv, "last_cancelled_draft", None)
    if not isinstance(snap, dict) or not snap.get("fields", {}).get("pending_product_sku"):
        return None
    try:
        ts = _utc(datetime.fromisoformat(str(snap.get("ts"))))
    except (TypeError, ValueError):
        return None
    if ts is None or (now or datetime.now(timezone.utc)) - ts > draft_ttl():
        return None
    return snap


def summary_line(snapshot: dict, product_name: str | None = None) -> str:
    """'Traditional choli Red, L × 2' — the one-line description of a snapshot (cart orders list every line)."""
    fields = snapshot.get("fields", {})
    cart = fields.get("cart_items") or []
    if cart:
        parts = []
        for item in cart:
            variant = ", ".join(x for x in (item.get("color"), item.get("size"), item.get("material")) if x)
            parts.append(f"{item.get('product_name') or product_name or 'item'}" + (f" {variant}" if variant else "") + f" × {item.get('qty')}")
        return "; ".join(parts)
    variant = ", ".join(x for x in (fields.get("selected_color"), fields.get("selected_size"), fields.get("selected_material")) if x)
    qty = fields.get("pending_order_quantity")
    line = product_name or fields.get("pending_product_sku") or "your item"
    if variant:
        line += f" {variant}"
    if qty:
        line += f" × {qty}"
    return line


async def save_cancelled_draft(db, conv) -> bool:
    """Persist the draft snapshot BEFORE the cancel wipes it. Best effort: a failure never blocks the cancel."""
    from app.services import conversation_service

    snap = build_snapshot(conv)
    if snap is None:
        return False
    try:
        await conversation_service.update_order_field(db, conv.id, "last_cancelled_draft", snap)
        conv.last_cancelled_draft = snap
        logger.info("DRAFT saved conv=%s sku=%s fields=%d", conv.id, snap["fields"].get("pending_product_sku"), len(snap["fields"]))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("cancelled-draft snapshot failed conv=%s: %s", getattr(conv, "id", None), exc)
        return False


async def restore_draft_fields(db, conv) -> dict | None:
    """Write the snapshot's fields back onto the conversation and clear the snapshot. Returns the fields (None if none)."""
    from app.services import conversation_service

    snap = restorable_draft(conv)
    if snap is None:
        return None
    fields = dict(snap["fields"])
    for name in DRAFT_FIELDS:
        value = fields.get(name)
        if name == "summary_shown":
            value = False        # the summary is re-rendered (and re-marked) by the restore reply
        await conversation_service.update_order_field(db, conv.id, name, value)
        setattr(conv, name, value)
    await clear_cancelled_draft(db, conv)
    return fields


async def clear_cancelled_draft(db, conv) -> None:
    """Forget the snapshot (restored, expired or no longer wanted)."""
    from app.services import conversation_service

    if getattr(conv, "last_cancelled_draft", None) is None:
        return
    try:
        await conversation_service.update_order_field(db, conv.id, "last_cancelled_draft", None)
        conv.last_cancelled_draft = None
    except Exception as exc:  # noqa: BLE001
        logger.error("clear cancelled draft failed conv=%s: %s", getattr(conv, "id", None), exc)


# ── "Cancel this order? (Yes / No)" state ────────────────────────────────────

def cancel_confirm_open(conv, now: datetime | None = None) -> bool:
    """True while a cancel confirmation question is waiting for its Yes/No."""
    at = _utc(getattr(conv, "cancel_confirm_at", None))
    ttl = timedelta(minutes=max(1, int(get_settings().cancel_confirm_ttl_minutes)))
    return at is not None and (now or datetime.now(timezone.utc)) - at <= ttl


async def open_cancel_confirm(db, conv) -> None:
    """Remember that the bot asked 'Cancel this order?' (stage and slots are untouched)."""
    from app.services import conversation_service

    now = datetime.now(timezone.utc)
    await conversation_service.update_order_field(db, conv.id, "cancel_confirm_at", now)
    conv.cancel_confirm_at = now


async def close_cancel_confirm(db, conv) -> None:
    """The question was answered (or ignored): forget it."""
    from app.services import conversation_service

    if getattr(conv, "cancel_confirm_at", None) is None:
        return
    try:
        await conversation_service.update_order_field(db, conv.id, "cancel_confirm_at", None)
        conv.cancel_confirm_at = None
    except Exception as exc:  # noqa: BLE001
        logger.error("close cancel confirm failed conv=%s: %s", getattr(conv, "id", None), exc)


# ── pipeline hook ────────────────────────────────────────────────────────────

async def run_recovery_guard(
    db, conv, client, message, user_text: str, sender_phone: str, wamid: str | None, stored_stage: str,
    language: str, record_usage,
):
    """
    Pipeline hook, right after the irrelevant-message guard (before every yes/no guard, which would misread the answer):

      * an open "Cancel this order?" question: yes -> cancel (snapshot saved), no -> the order carries on, anything
        else closes the question and is handled normally;
      * a cancelled draft still inside its restore window and a "continue" / "yes" while nothing else is open ->
        the draft is restored and the next step re-asked.

    Returns a PipelineResult, or None to carry on. Text and button messages only.
    """
    if getattr(message, "type", "text") not in ("text", "interactive") or client is None or conv is None:
        return None
    text = user_text or ""
    confirm_open = cancel_confirm_open(conv)
    resume = (
        restorable_draft(conv) is not None and stored_stage not in _ORDER_STAGES
        and not getattr(conv, "pending_product_sku", None) and not getattr(conv, "interrupted_sku", None)
        and not getattr(conv, "pending_choice_skus", None) and is_resume_request(text)
    )
    if not confirm_open and not resume:
        return None
    from app.services import catalogue_service, conversation_flow
    from app.services import router_actions as ra

    pinned = None
    variant_info: dict = {}
    if conv.pending_product_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, conv.pending_product_sku)
        if pinned is not None:
            try:
                variant_info = await catalogue_service.get_product_variant_info(db, pinned)
            except Exception as exc:  # noqa: BLE001
                logger.error("recovery guard variant_info error: %s", exc)
    if conv.channel == "whatsapp" and not getattr(conv, "mobile_number", None):
        conv.mobile_number = sender_phone      # same in-memory fill as the router front door: the sender IS the number
    next_slot = (
        conversation_flow.get_next_required_slot(conv, variant_info)
        if pinned is not None and stored_stage in ("order_collection", "awaiting_final_confirmation") else None
    )
    rc = ra.RouterCtx(
        db=db, conv=conv, client=client, message=message, user_text=text, sender_phone=sender_phone, wamid=wamid,
        stage=stored_stage, language=language, record_usage=record_usage, ctx=None, all_products=[], pinned_product=pinned,
        variant_info=variant_info, next_slot=next_slot, profile=None,
    )
    if confirm_open:
        if is_yes_answer(text) or has_explicit_cancel_keyword(text):
            await close_cancel_confirm(db, conv)
            logger.info("CANCEL_CONFIRM conv=%s answer=yes", conv.id)
            return (await ra.handle_cancel_order(rc, None)).early_result
        if cw.matches("no", text) or cw.matches("continue", text) or _contains_phrase(_norm(text), ("keep", "keep it", "keep current", "dont cancel", "do not cancel")):
            await close_cancel_confirm(db, conv)
            logger.info("CANCEL_CONFIRM conv=%s answer=no", conv.id)
            return (await ra.handle_cancel_declined(rc)).early_result
        await close_cancel_confirm(db, conv)       # something else: the question is dropped, the message is handled normally
        if not resume:
            return None
    logger.info("DRAFT restore requested conv=%s text=%r", conv.id, text[:40])
    return (await ra.handle_restore_draft(rc)).early_result
