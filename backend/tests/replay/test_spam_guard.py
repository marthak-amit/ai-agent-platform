"""
Replay tests for the irrelevant-message guard (real webhook → pipeline → Postgres, scripted LLM).

Noise (emoji-only, off-topic) three times in a row → ONE final notice, then silence with NO LLM calls for the mute
window; cheap/human/engaged messages still work; the streak resets on real messages; nothing is ever muted mid-order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all
from tests.replay.test_out_of_scope import WEATHER, _only_conv, _stub_off_topic_llm
from tests.replay.test_router_v2 import (  # noqa: F401 — fixtures + helpers shared with the router suite
    _no_reply_delay, get_conv, messages, prime_conv, router, say, seed_shop,
)

# ruff: noqa: F811
pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """These tests send many messages in a few seconds; the per-phone rate limiter (5 / 10 s) is not what is under test."""
    monkeypatch.setattr("app.routers.webhook._RATE_LIMIT_MESSAGES", 1000)


def ph(n: str) -> tuple[str, str]:
    """(phone, phone_number_id) unique per test."""
    return f"91950090{n}", f"789{n}"


async def silent(http, captured, phone, pnid, text) -> bool:
    """Send a message; True when the bot sent nothing back."""
    return await say(http, captured, phone, pnid, text) == ""


async def test_three_emoji_messages_get_nudge_nudge_final_and_never_call_the_llm(replay_http, replay_session, router, monkeypatch):
    """😂😂😂 ×3: fixed nudge, fixed nudge, ONE final notice + mute — zero router/LLM calls (the router would fail on any call)."""
    phone, pnid = ph("0001")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)

    r1 = await say(replay_http, captured, phone, pnid, "😂😂😂")
    r2 = await say(replay_http, captured, phone, pnid, "😂")
    r3 = await say(replay_http, captured, phone, pnid, "🤣🤣")

    assert "I didn't understand that" in r1 and "I didn't understand that" in r2
    assert "pause replies for about 15 minutes" in r3 and "talk to someone" in r3
    assert not router.calls
    conv = await _only_conv(replay_session, client.id)
    assert conv.off_topic_count == 3 and conv.spam_muted_until is not None
    assert conv.spam_muted_until > datetime.now(timezone.utc) + timedelta(minutes=14)


async def test_after_the_mute_free_text_is_saved_but_not_answered_and_costs_nothing(replay_http, replay_session, router, monkeypatch):
    """4th+ message (a real question, an off-topic one, an emoji): no reply, no router call, but stored in the transcript."""
    phone, pnid = ph("0002")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    for text in ("😂", "😂😂", "😂😂😂"):
        await say(replay_http, captured, phone, pnid, text)
    sent = len(captured)

    for text in ("do you have sarees?", "who is narendra modi", "😂", "👍"):
        assert await silent(replay_http, captured, phone, pnid, text), text

    assert len(captured) == sent and not router.calls
    conv = await _only_conv(replay_session, client.id)
    stored_users = [m.content for m in await messages(replay_session, conv.id) if m.role == "user"]
    assert stored_users[-4:] == ["do you have sarees?", "who is narendra modi", "😂", "👍"]


async def test_during_the_mute_a_sku_and_a_human_request_still_work(replay_http, replay_session, router, monkeypatch):
    """Engaged / cheap messages are not punished: an exact SKU gets its product card; 'talk to someone' hands off to staff."""
    phone, pnid = ph("0003")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    for text in ("😂", "😂😂", "😂😂😂"):
        await say(replay_http, captured, phone, pnid, text)

    card = await say(replay_http, captured, phone, pnid, "SR20001")
    human = await say(replay_http, captured, phone, pnid, "talk to someone")

    assert "Green Cotton Saree" in card
    assert human != ""
    conv = await _only_conv(replay_session, client.id)
    assert conv.ai_enabled is False                                   # handed to the team


async def test_the_mute_ends_by_itself_and_the_next_message_is_answered_normally(replay_http, replay_session, router, monkeypatch):
    """Once spam_muted_until has passed, the streak restarts from zero and the message goes through the router as usual."""
    phone, pnid = ph("0004")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    for text in ("😂", "😂😂", "😂😂😂"):
        await say(replay_http, captured, phone, pnid, text)
    conv = await _only_conv(replay_session, client.id)
    conv.spam_muted_until = datetime.now(timezone.utc) - timedelta(seconds=5)
    await replay_session.commit()
    router.when("show me sarees", "faq", {"query": "x"})

    reply = await say(replay_http, captured, phone, pnid, "show me sarees")

    assert "talk to someone" in reply and [c["message"] for c in router.calls] == ["show me sarees"]
    conv = await _only_conv(replay_session, client.id)
    assert conv.spam_muted_until is None and conv.off_topic_count == 0


async def test_three_off_topic_questions_through_the_router_end_in_the_final_notice_then_silence(replay_http, replay_session, router, monkeypatch):
    """The LLM cost case: 3 out_of_scope calls, then the 4th message is dropped BEFORE the router (no 4th call)."""
    phone, pnid = ph("0005")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    for text in ("who won the match", "tell me a joke", "what is the capital of france"):
        router.when(text, "out_of_scope", confidence=0.95)

    replies = [await say(replay_http, captured, phone, pnid, t)
               for t in ("who won the match", "tell me a joke", "what is the capital of france")]
    calls_after_three = len(router.calls)

    assert "I can only help with Router Test Store" in replies[0] and "pause replies" not in replies[0]
    assert "pause replies for about 15 minutes" in replies[2]
    assert await silent(replay_http, captured, phone, pnid, "ok one more joke please")
    assert len(router.calls) == calls_after_three == 3                # the 4th message never reached the LLM
    assert (await _only_conv(replay_session, client.id)).spam_muted_until is not None


async def test_a_real_message_between_noise_resets_the_streak(replay_http, replay_session, router, monkeypatch):
    """junk, junk, a real product request, junk, junk → still not muted (the streak is CONSECUTIVE)."""
    phone, pnid = ph("0006")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("show me sarees", "search_catalog", {"query": "saree"})

    await say(replay_http, captured, phone, pnid, "😂")
    await say(replay_http, captured, phone, pnid, "😂")
    await say(replay_http, captured, phone, pnid, "show me sarees")
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 0
    await say(replay_http, captured, phone, pnid, "😂")
    await say(replay_http, captured, phone, pnid, "😂")

    conv = await _only_conv(replay_session, client.id)
    assert conv.off_topic_count == 2 and conv.spam_muted_until is None


async def test_thanks_emojis_are_answered_as_before_and_never_counted(replay_http, replay_session, router, monkeypatch):
    """👍 / 🙏 are normal thanks: they reach the router like any message and add nothing to the streak."""
    phone, pnid = ph("0007")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("👍", "smalltalk", reply_hint="You're welcome! 😊")
    router.when("🙏", "smalltalk", reply_hint="You're welcome! 😊")

    for text in ("👍", "🙏", "👍", "🙏", "👍"):
        await say(replay_http, captured, phone, pnid, text)

    conv = await _only_conv(replay_session, client.id)
    assert conv.off_topic_count == 0 and conv.spam_muted_until is None


async def test_noise_during_an_order_is_ignored_and_never_mutes_or_blocks_the_order(replay_http, replay_session, router, monkeypatch):
    """At the colour question 😂 ×4: no reply, no LLM, no mute — and the customer's real answer is accepted afterwards."""
    phone, pnid = ph("0008")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042", pending_order_quantity=2)

    for _ in range(4):
        assert await silent(replay_http, captured, phone, pnid, "😂😂")
    await say(replay_http, captured, phone, pnid, "Pink")

    conv = await get_conv(replay_session, conv_id)
    assert not router.calls and conv.spam_muted_until is None and conv.selected_color == "Pink"


async def test_keyword_pipeline_shops_get_the_same_final_notice_and_mute(replay_http, replay_session, monkeypatch):
    """Router OFF: off-topic ×3 through the keyword path's classifier → final notice + mute; the 4th message is dropped."""
    phone, pnid = ph("0009")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid, router_on=False)
    chat = _stub_off_topic_llm(monkeypatch, verdict="YES")

    replies = [await say(replay_http, captured, phone, pnid, f"{WEATHER} {i}") for i in range(3)]
    calls = chat.call_count

    assert "pause replies" not in replies[0] and "jawab nahi aapu" in replies[2]
    assert await silent(replay_http, captured, phone, pnid, "ahmedabad ma vatavaran kevu chhe aaje")
    assert chat.call_count == calls                                    # the dropped message cost nothing
    assert (await _only_conv(replay_session, client.id)).spam_muted_until is not None
