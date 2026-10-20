"""Unit tests for message batching (no DB): decisions, the quiet window, caps, ordering, dedupe, cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import message_batcher as mb

KEY = ("whatsapp", 1, "919800000001")
FAST = dict(window=0.12, extended_window=0.3, max_wait=2.0, max_messages=8)


@pytest.fixture(autouse=True)
def _clean():
    """Batcher state is module-global: start and end every test empty."""
    mb.reset_state()
    yield
    mb.reset_state()


def conv(**kw):
    """A conversation in a browsing stage with no open question."""
    base = dict(current_stage="greeting", pending_choice_skus=None, interrupted_sku=None, summary_shown=False, ai_enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


# ── decisions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("I want", True), ("I want a saree,", True), ("if", True), ("mujhe", True), ("mane", True), ("red colour and", True),
    ("pan", True), ("mare", True), ("mare bija", True), ("Pan mare", True), ("choli jova", True), ("another", True),
    ("I want to buy banarasi silk sadi", False), ("if red colour is there then good", False),
    ("choli jova che", False), ("hi", False), ("Do you have sarees?", False), ("show me kurtis.", False), ("red saree chahiye", False), ("", False),
])
def test_looks_unfinished(text, expected):
    """Fragments that end like the start of a sentence get the longer window; complete ones the short one."""
    assert mb.looks_unfinished(text) is expected


@pytest.mark.parametrize("stage", ["greeting", "product_inquiry", "qualification", "objection_handling", "offer_making", "completed"])
def test_browsing_text_is_eligible(stage):
    """Free text while browsing may lead a batch."""
    assert mb.eligible(conv(current_stage=stage), "I want to buy banarasi silk sadi") is True


@pytest.mark.parametrize("c,text", [
    (conv(current_stage="order_collection"), "my name is Ravi Patel"),            # answering an order question
    (conv(current_stage="awaiting_final_confirmation"), "yes please deliver fast"),
    (conv(current_stage="payment"), "I have paid just now"),
    (conv(interrupted_sku="KU1"), "keep the old one"),
    (conv(summary_shown=True), "change quantity please"),
    (conv(ai_enabled=False), "hello team"),                                       # bot paused
    (conv(), "KU84017"), (conv(), "send me KU84017 in red"),                      # a SKU is a complete reference
    (conv(), "yes"), (conv(), "no"), (conv(), "2"), (conv(), "COD"),              # bare answers
    (conv(), "Hi, I want to order from your catalogue\nSKU: KU84017"),            # catalogue order paste
    (conv(), "   "), (None, "hello there"),
])
def test_not_eligible(c, text):
    """Order answers, open prompts, SKUs, bare words, pastes, paused bot and empty text are never held back."""
    assert mb.eligible(c, text) is False


def test_an_open_which_one_menu_does_not_block_batching():
    """The user's real case: after a product list ('reply with the number') the customer answers in word fragments."""
    menu = conv(current_stage="product_inquiry", pending_choice_skus='["DP3286", "KU84017"]')
    for text in ("Pan mare", "Bija", "Choli", "Jova che", "choli jova che"):
        assert mb.eligible(menu, text) is True, text
    for text in ("2", "1", "DP3286", "yes", "no"):                # the menu's own answers are never batched
        assert mb.eligible(menu, text) is False, text


@pytest.mark.parametrize("text,expected", [
    ("pan", True), ("choli", True), ("Bija", True), ("Jova che", True), ("mare bija", True), ("saree", True),
    ("choli jova che", False), ("show me red sarees", False), ("hi", False), ("thanks", False), ("ok", False),
    ("saree?", False), ("kya hai?", False), ("", False),
])
def test_looks_short(text, expected):
    """One/two-word fragments (not greetings/thanks/yes/no, not questions) get the middle window."""
    assert mb.looks_short(text) is expected


def test_quiet_window_picks_the_right_tier():
    """unfinished > short > complete."""
    args = (3.5, 5.0, 7.0)
    assert mb.quiet_window("I want", *args) == 7.0
    assert mb.quiet_window("mare bija", *args) == 7.0
    assert mb.quiet_window("choli", *args) == 5.0
    assert mb.quiet_window("choli jova che", *args) == 3.5
    assert mb.quiet_window("hi", *args) == 3.5
    assert mb.quiet_window("choli", 3.5, 2.0, 7.0) == 3.5           # never shorter than the base window


@pytest.mark.parametrize("text", ["2", "yes", "COD", "KU84017", "Hi, I want to order\nSKU: KU84017", "   "])
def test_batchable_text_rejects_bare_answers_skus_and_pastes(text):
    """Texts that answer an open question or name a product exactly are never merged into a batch."""
    assert mb.batchable_text(text) is False


# ── the turn ─────────────────────────────────────────────────────────────────

async def _leader(texts: list[str], gaps: list[float], **kw):
    """Leader sends texts[0]; followers send the rest after the given gaps; returns (leader Turn, join results)."""
    params = {**FAST, **kw}
    joined: list[bool] = []
    out: dict = {}

    async def lead():
        async with mb.turn(KEY, enabled=True, lead=True, text=texts[0], wamid="w0", **params) as t:
            out["turn"] = t

    task = asyncio.create_task(lead())
    await asyncio.sleep(0)
    for i, (text, gap) in enumerate(zip(texts[1:], gaps), 1):
        await asyncio.sleep(gap)
        joined.append(mb.try_join(KEY, text, f"w{i}", max_messages=params["max_messages"]))
    await task
    return out["turn"], joined


async def test_the_requirement_example_is_merged_into_one_turn():
    """The four fragments of one request become ONE text, in order; the followers are told they joined."""
    texts = ["I want", "to buy", "banarasi silk sadi", "if red colour is there then good"]
    t, joined = await _leader(texts, [0.05, 0.05, 0.05])
    assert t.text == "I want to buy banarasi silk sadi if red colour is there then good"
    assert t.merged == 4 and joined == [True, True, True]
    assert t.waited >= 0.15


async def test_every_follower_restarts_the_quiet_window():
    """Messages arriving within the window keep extending the wait; the batch closes only after real silence."""
    t, joined = await _leader(["red saree", "silk one", "banarasi"], [0.1, 0.1], window=0.15)
    assert t.merged == 3 and joined == [True, True] and t.waited >= 0.35


async def test_a_single_complete_message_waits_only_the_short_window():
    """No followers: processed after about the base window, text unchanged."""
    t, _ = await _leader(["show me red sarees please"], [])
    assert t.text == "show me red sarees please" and t.merged == 1
    assert 0.1 <= t.waited < 0.28


async def test_an_unfinished_fragment_waits_the_longer_window():
    """'I want' (unfinished) holds for the extended window so the rest of the sentence can arrive."""
    t, _ = await _leader(["I want"], [], window=0.1, extended_window=0.35)
    assert t.waited >= 0.33


async def test_hard_cap_bounds_the_wait_for_a_chatty_sender():
    """A sender who keeps typing cannot hold the reply past max_wait."""
    t, _ = await _leader(["a b", "c d", "e f", "g h", "i j", "k l"], [0.1] * 5, window=0.25, max_wait=0.35)
    assert t.waited < 0.6 and t.merged >= 2


async def test_message_cap_closes_the_batch_and_extra_messages_are_not_joined():
    """At max_messages the leader stops waiting; a further message is not swallowed (try_join says no)."""
    out = {}

    async def lead():
        async with mb.turn(KEY, enabled=True, lead=True, text="one", wamid="w0", window=5.0, extended_window=5.0,
                           max_wait=5.0, max_messages=3) as t:
            out["t"] = t

    task = asyncio.create_task(lead())
    await asyncio.sleep(0)
    assert mb.try_join(KEY, "two", "w1", max_messages=3) and mb.try_join(KEY, "three", "w2", max_messages=3)
    await asyncio.wait_for(task, timeout=1.0)           # returned long before the 5s window
    assert out["t"].text == "one two three"
    assert mb.try_join(KEY, "four", "w3", max_messages=3) is False


async def test_try_join_without_an_open_batch_is_false():
    """No leader waiting → the message must be handled on its own."""
    assert mb.try_join(KEY, "hello", "w1", max_messages=8) is False


async def test_disabled_is_a_pure_passthrough_with_no_state():
    """enabled=False: original text, zero wait, nothing registered."""
    async with mb.turn(KEY, enabled=False, lead=True, text="hi", wamid="w", **FAST) as t:
        assert t.text == "hi" and t.waited == 0 and t.merged == 1
        assert mb._slots == {}
    assert mb.seen_recently("w") is False


async def test_an_unbatchable_message_ends_the_wait_and_runs_after_the_leader():
    """A tap/photo/SKU arriving mid-wait flushes the batch immediately and is processed AFTER the batch's turn."""
    order: list[str] = []

    async def leader():
        async with mb.turn(KEY, enabled=True, lead=True, text="I want", wamid="w0", window=5.0, extended_window=5.0,
                           max_wait=5.0, max_messages=8) as t:
            order.append(f"leader:{t.text}")
            await asyncio.sleep(0.05)
            order.append("leader-done")

    async def tap():
        async with mb.turn(KEY, enabled=True, lead=False, text="KU84017", wamid="w1", window=5.0, extended_window=5.0,
                           max_wait=5.0, max_messages=8) as t:
            order.append(f"tap:{t.text}")

    lead_task = asyncio.create_task(leader())
    await asyncio.sleep(0.02)
    await asyncio.wait_for(asyncio.gather(lead_task, tap()), timeout=2.0)
    assert order == ["leader:I want", "leader-done", "tap:KU84017"]


async def test_turns_of_one_sender_never_overlap_but_different_senders_do():
    """Same key → serialised; another sender runs in parallel."""
    running = {"n": 0, "max": 0}

    async def one(key, text):
        async with mb.turn(key, enabled=True, lead=False, text=text, wamid=text, **FAST):
            running["n"] += 1
            running["max"] = max(running["max"], running["n"])
            await asyncio.sleep(0.05)
            running["n"] -= 1

    await asyncio.gather(one(KEY, "a"), one(KEY, "b"), one(KEY, "c"))
    assert running["max"] == 1
    running["max"] = 0
    await asyncio.gather(one(KEY, "a"), one(("whatsapp", 1, "other"), "b"))
    assert running["max"] == 2


async def test_a_new_burst_during_processing_waits_its_turn_and_gets_its_own_batch():
    """Messages arriving while the previous batch is being processed form a second batch that runs afterwards."""
    texts: list[str] = []

    async def lead(text, delay):
        await asyncio.sleep(delay)
        async with mb.turn(KEY, enabled=True, lead=True, text=text, wamid=text, **FAST) as t:
            texts.append(t.text)
            await asyncio.sleep(0.3)

    first = asyncio.create_task(lead("first message", 0))
    second = asyncio.create_task(lead("second message", 0.2))     # arrives while the first turn is running
    await asyncio.sleep(0.25)
    assert mb.try_join(KEY, "and more", "w9", max_messages=8)        # joins the SECOND batch
    await asyncio.gather(first, second)
    assert texts == ["first message", "second message and more"]


async def test_state_is_cleaned_up_and_exceptions_release_the_lock():
    """No slot is left behind (also after an error inside the turn), and the next turn is not blocked."""
    with pytest.raises(RuntimeError):
        async with mb.turn(KEY, enabled=True, lead=True, text="boom", wamid="w", **FAST):
            raise RuntimeError("pipeline crashed")
    assert mb._slots == {}
    async with mb.turn(KEY, enabled=True, lead=False, text="next", wamid="w2", **FAST) as t:
        assert t.text == "next"
    assert mb._slots == {}


def test_wamids_of_batched_messages_are_remembered_for_retry_dedupe():
    """A Meta retry of a message already taken into a batch is recognised; unknown ids and None are not."""
    assert not mb.seen_recently("wamid.A") and not mb.seen_recently(None)
    mb._remember("wamid.A")
    assert mb.seen_recently("wamid.A") and not mb.seen_recently("wamid.B")


def test_seen_ids_are_bounded():
    """The dedupe memory cannot grow without limit."""
    for i in range(mb._SEEN_MAX + 50):
        mb._remember(f"w{i}")
    assert len(mb._seen) <= mb._SEEN_MAX


async def test_the_real_gujarati_fragments_are_merged_even_with_slow_typing_gaps():
    """'Pan' / 'mare bija' / 'choli jova che' typed with pauses (scaled: window 0.35s, gaps 0.25s) → ONE turn."""
    t, joined = await _leader(
        ["Pan", "mare bija", "choli jova che"], [0.25, 0.25], window=0.35, short_window=0.5, extended_window=0.7,
    )
    assert t.text == "Pan mare bija choli jova che" and t.merged == 3 and joined == [True, True]


async def test_a_short_fragment_waits_the_middle_window():
    """'choli' alone is probably the start of a request: it waits longer than a full sentence."""
    t, _ = await _leader(["choli"], [], window=0.1, short_window=0.3, extended_window=0.6)
    assert 0.28 <= t.waited < 0.55


async def test_a_bare_number_follower_is_not_swallowed_and_ends_the_wait():
    """With a batch pending, '2' (a menu pick) is not joined: try_join says no, and its own turn runs after the batch."""
    out = {}

    async def lead():
        async with mb.turn(KEY, enabled=True, lead=True, text="Pan mare", wamid="w0", window=5.0, short_window=5.0,
                           extended_window=5.0, max_wait=5.0, max_messages=8) as t:
            out["lead"] = t.text

    task = asyncio.create_task(lead())
    await asyncio.sleep(0.02)
    assert mb.try_join(KEY, "2", "w1", max_messages=8) is False
    async with mb.turn(KEY, enabled=True, lead=False, text="2", wamid="w1", window=5.0, short_window=5.0,
                       extended_window=5.0, max_wait=5.0, max_messages=8) as t2:
        assert t2.text == "2" and out["lead"] == "Pan mare"         # the batch ran first, then the pick
    await task


@pytest.mark.parametrize("text", ["😂", "😂😂😂", "???", "hahaha", "👍", "🙏🙏", "❤️"])
def test_emoji_noise_and_thanks_emojis_are_never_batched_or_waited_for(text):
    """Nothing to wait for: they are handled at once (and end a pending batch's wait if one is open)."""
    assert mb.batchable_text(text) is False and mb.eligible(conv(), text) is False


def test_is_exact_reference_is_the_cheap_text_check():
    """SKUs, bare numbers/yes/no/COD and catalogue pastes are exact references; words and emoji are not."""
    for text in ("KU84017", "2", "yes", "no", "COD", "UPI", "Hi, I want to order\nSKU: KU84017"):
        assert mb.is_exact_reference(text) is True, text
    for text in ("saree", "pan mare", "😂", "", "   "):
        assert mb.is_exact_reference(text) is False, text
