"""
Message batching — one answer for a burst of messages.

Customers often type one request across several messages:

    "I want" · "to buy" · "banarasi silk sadi" · "if red colour is there then good"

Answering each fragment separately produces three confused replies. WhatsApp / Instagram never tell a business
that the customer is still typing, so the only signal is silence: the FIRST message of a burst becomes the
*leader*, waits until the sender has been quiet for a short window, and then runs ONE turn on all the texts joined
together. Later messages (*followers*) just append their text to the leader's batch and return 200 at once.

Rules that keep the existing flows safe:
  * Only plain text from a conversation in a browsing stage is batched (`eligible`). Mid-order answers
    (name / address / colour / quantity / payment), open "which one?" menus, button taps, SKUs, bare yes/no or
    numbers, catalogue "Order" pastes, images and voice notes are never held back.
  * The quiet window is short (`MESSAGE_BATCH_WINDOW_SECONDS`), longer after a one/two-word fragment and longest when it looks unfinished
    ("I want", "to", a trailing comma); a hard cap (`MESSAGE_BATCH_MAX_WAIT_SECONDS`) and a message cap bound the wait.
  * A message that is not batched but arrives while a batch is pending ends the wait early and queues behind it, so
    order is preserved (text, then the photo that followed it).
  * Turns of one sender are serialised (one lock per sender) — two bursts never run concurrently.
  * Time already spent waiting is credited against the pipeline's artificial reply delay (`reply_delay_credit`),
    so a single message is not slower overall.
  * Wamids of batched messages are remembered so a Meta retry of any of them is not processed twice.

State is in-process (like the per-phone rate limiter): correct for the single-instance deployment; with several
instances the buffer would have to move to Redis. `MESSAGE_BATCH_WINDOW_SECONDS=0` switches everything off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("app.services.message_batcher")

#: Stored stages in which a burst may be merged (browsing; never an order in progress).
BATCHABLE_STAGES = frozenset({
    "greeting", "product_inquiry", "qualification", "objection_handling", "offer_making", "completed", "off_topic",
})

# A fragment that ends like this is almost certainly continued: wait the longer window.
_UNFINISHED_ENDINGS = (",", ";", ":", "-", "–", "…", "...", "&", "+", "/")
_CONNECTIVES = frozenset({
    # English — words that cannot end a sentence
    "i", "i'd", "id", "want", "wanna", "need", "to", "and", "or", "but", "if", "for", "with", "of", "in", "on", "at",
    "a", "an", "the", "my", "would", "can", "could", "have", "is", "are", "any", "some", "because", "which", "what",
    "another", "other", "different",
    # Hindi / Hinglish
    "mujhe", "mereko", "mere", "meri", "mera", "hume", "humko", "humein", "aur", "ya", "toh", "agar", "jo", "ke", "ki",
    "ka", "ko", "se", "mein", "par", "bhi", "lekin", "magar", "dusra", "dusri", "dusre", "alag", "koi",
    # Gujarati (Roman)
    "mane", "mare", "mari", "mara", "amne", "tamne", "ane", "athva", "nu", "ni", "ma", "mate", "pan", "paan", "pachhi",
    "bija", "biji", "bijo", "bijane", "alag", "jova", "joyi",
})

_SEEN_TTL_SECONDS = 600.0
_SEEN_MAX = 5000
_LOCK_WAIT_SECONDS = 90.0     # never let a stuck turn block a customer's later messages forever

Key = tuple


@dataclass
class _Batch:
    """The texts a leader is collecting for one sender."""

    texts: list[str]
    started: float
    last: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    flush: bool = False        # a non-batched message wants the leader to stop waiting


@dataclass
class _Slot:
    """Per-sender state: the turn lock, the batch being collected (if any) and how many callers use the slot."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    batch: _Batch | None = None
    refs: int = 0


@dataclass(frozen=True)
class Turn:
    """What a caller gets for its turn: the text to process and how it was assembled."""

    text: str
    waited: float = 0.0        # seconds spent waiting (quiet window + queueing behind an earlier turn)
    merged: int = 1            # how many messages the text was assembled from


_slots: dict[Key, _Slot] = {}
_seen: "OrderedDict[str, float]" = OrderedDict()


# ── decisions ────────────────────────────────────────────────────────────────

def looks_unfinished(text: str) -> bool:
    """True when the fragment reads like the start of a sentence ("I want", "mare bija", "to buy,") — wait longest."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.endswith(_UNFINISHED_ENDINGS):
        return True
    if lowered.endswith(("?", "!", ".", "।")):
        return False
    last = lowered.split()[-1].strip("'\"()[]")
    return last in _CONNECTIVES


def looks_short(text: str) -> bool:
    """
    A one- or two-word fragment ("pan", "choli", "bija") that is not a greeting / thanks / yes / no.

    Customers split a sentence into tiny pieces, and a lone word is far more often the start of a request than a
    finished one, so it waits longer than a full sentence.
    """
    from app.services import command_words, indic_vocab

    stripped = (text or "").strip()
    if not stripped or stripped.endswith(("?", "!", ".", "।")):
        return False
    return len(indic_vocab.tokenize(stripped)) <= 2 and not command_words.is_smalltalk_only(stripped)


def quiet_window(text: str, window: float, short_window: float, extended_window: float) -> float:
    """Seconds of silence to wait after this latest fragment: unfinished > short > complete sentence."""
    if looks_unfinished(text):
        return extended_window
    if looks_short(text):
        return max(window, short_window)
    return window


def is_exact_reference(text: str) -> bool:
    """
    A bare number / yes / no / COD / UPI, a SKU, or a catalogue "Order" paste: it answers an open question or names a
    product exactly, so it resolves for ₹0 on its own.
    """
    from app.services import catalogue_service, conversation_flow, intent_router

    text = (text or "").strip()
    if not text:
        return False
    if conversation_flow.is_catalogue_order_paste(text) or catalogue_service.extract_skus_from_text(text):
        return True
    norm = intent_router.normalize_option(text)
    bare = {intent_router.normalize_option(w) for w in intent_router._GATE_WORDS | intent_router._PAYMENT_WORDS}
    return bool(norm) and (norm.isdigit() or norm in bare)


def batchable_text(text: str) -> bool:
    """
    May this text be part of a batch at all? Never an exact reference (`is_exact_reference`), an emoji-only /
    laughter message or an acknowledgement emoji (nothing to wait for), or an empty text — those are handled on their
    own (and, if a batch is waiting, end its wait).
    """
    from app.services import spam_guard

    text = (text or "").strip()
    if not text or spam_guard.classify_text(text) != "text":
        return False
    return not is_exact_reference(text)


def eligible(conv, text: str, *, skip_social: bool = False, product_names: "frozenset[str] | None" = None) -> bool:
    """
    May this text start (lead) a batch? Only free-flowing browsing text in a conversation with no open order step.

    Never: an order in progress, an interrupted-SKU prompt, a paused bot, or a text `batchable_text` rejects. An open
    "which one?" menu does NOT block batching — its answers are bare numbers/SKUs, which are never batched anyway,
    and a customer who answers it with words ("pan mare bija choli jova che") should get one reply, not one per word.

    `skip_social` (agent v3): a bare greeting / thanks / yes / no, a product's exact name (`product_names`, normalised), a request
    for the product's photos or a side question (returns? COD? delivery time?) is a complete message answered for ₹0 — there is
    nothing to wait for, so it is not held for more fragments.
    """
    if conv is None or not batchable_text(text):
        return False
    if skip_social:
        from app.services import agent_fast_paths, catalogue_snapshot, command_words, indic_vocab, side_questions

        tokens = indic_vocab.tokenize(text)
        is_photo_request = len(tokens) <= 4 and bool(set(tokens) & agent_fast_paths.IMAGE_WORDS)
        if (
            command_words.is_smalltalk_only(text) or is_photo_request or side_questions.detect_topic(text) is not None
            or (product_names and catalogue_snapshot.normalize_name(text) in product_names)
        ):
            return False
    if getattr(conv, "current_stage", None) not in BATCHABLE_STAGES:
        return False
    if getattr(conv, "interrupted_sku", None):
        return False
    if getattr(conv, "summary_shown", False) or getattr(conv, "ai_enabled", True) is False:
        return False
    return True


# ── in-flight dedupe ─────────────────────────────────────────────────────────

def _remember(wamid: str | None) -> None:
    """Remember a message id (bounded, expiring) so a Meta retry of a batched message is not processed again."""
    if not wamid:
        return
    now = time.monotonic()
    _seen[wamid] = now
    _seen.move_to_end(wamid)
    while _seen and (len(_seen) > _SEEN_MAX or now - next(iter(_seen.values())) > _SEEN_TTL_SECONDS):
        _seen.popitem(last=False)


def seen_recently(wamid: str | None) -> bool:
    """True when this message id was already taken into a batch in the last few minutes."""
    if not wamid:
        return False
    stamp = _seen.get(wamid)
    return stamp is not None and time.monotonic() - stamp <= _SEEN_TTL_SECONDS


# ── batching ─────────────────────────────────────────────────────────────────

def try_join(key: Key, text: str, wamid: str | None, *, max_messages: int) -> bool:
    """
    Add `text` to the batch a leader is collecting for `key`. True = joined (the caller returns 200 immediately and
    does nothing else); False = no batch is open (or it is full) → the caller handles the message itself.
    """
    slot = _slots.get(key)
    batch = slot.batch if slot else None
    if batch is None or len(batch.texts) >= max_messages or not batchable_text(text):
        return False       # no batch open / full / a bare answer or SKU: handle it on its own (it ends the wait)
    batch.texts.append(text.strip())
    batch.last = time.monotonic()
    batch.wake.set()
    _remember(wamid)
    logger.info("BATCH join key=%s fragments=%d", key[-1], len(batch.texts))
    return True


async def _collect(
    batch: _Batch, window: float, short_window: float, extended: float, max_wait: float, max_messages: int,
) -> None:
    """Wait until the sender has been quiet for the window (longer after an unfinished fragment), or a cap is hit."""
    while True:
        if batch.flush or len(batch.texts) >= max_messages:
            return
        quiet = quiet_window(batch.texts[-1], window, short_window, extended)
        remaining = min(batch.last + quiet, batch.started + max_wait) - time.monotonic()
        if remaining <= 0:
            return
        batch.wake.clear()
        try:
            await asyncio.wait_for(batch.wake.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def turn(
    key: Key, *, enabled: bool, lead: bool, text: str, wamid: str | None, window: float, extended_window: float,
    max_wait: float, max_messages: int, short_window: float | None = None,
):
    """
    Run one customer turn: `async with turn(...) as t:` then process `t.text`.

    * disabled → `t.text` is the original text, no waiting, no lock;
    * `lead` → open a batch, wait out the quiet window, process the joined text;
    * not `lead` → end any pending batch's wait early, queue behind it, process the original text.
    Every enabled turn holds the sender's lock while it runs, so a sender's turns never overlap.
    """
    if not enabled:
        yield Turn(text=text)
        return
    slot = _slots.setdefault(key, _Slot())
    slot.refs += 1
    acquired = False
    started = time.monotonic()
    batch: _Batch | None = None
    try:
        _remember(wamid)
        if lead and slot.batch is None:
            # Open the batch FIRST (so messages typed while an earlier turn is still running can join it) and take our
            # place in the lock queue before collecting — a message that arrives later can then never overtake us.
            batch = _Batch(texts=[(text or "").strip()], started=started, last=started)
            slot.batch = batch
        elif slot.batch is not None:
            slot.batch.flush = True          # an unbatchable message (tap, photo, SKU…) must not sit behind the wait
            slot.batch.wake.set()
        try:
            await asyncio.wait_for(slot.lock.acquire(), timeout=_LOCK_WAIT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            logger.error("BATCH lock wait timed out key=%s — processing without the lock", key[-1])
        merged_text, merged = text, 1
        if batch is not None:
            try:
                await _collect(
                    batch, window, window if short_window is None else short_window, extended_window, max_wait, max_messages,
                )
            finally:
                slot.batch = None            # closed: a later message starts a new batch behind this one
            merged, merged_text = len(batch.texts), " ".join(batch.texts)
        waited = time.monotonic() - started
        if merged > 1:
            logger.info("BATCH key=%s merged=%d waited=%.1fs", key[-1], merged, waited)
        yield Turn(text=merged_text, waited=waited, merged=merged)
    finally:
        if batch is not None and slot.batch is batch:
            slot.batch = None                # cancelled / failed before collecting finished
        if acquired:
            slot.lock.release()
        slot.refs -= 1
        if slot.refs <= 0 and slot.batch is None:
            _slots.pop(key, None)


def reset_state() -> None:
    """Forget all pending batches and remembered ids (tests)."""
    _slots.clear()
    _seen.clear()
