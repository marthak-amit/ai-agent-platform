"""
Replay tests for message batching (real webhook → pipeline → Postgres, scripted LLM).

A customer's burst of typed messages must be answered ONCE, from the merged text; order answers, taps and
retries must behave exactly as before. Batching is off in the suite by default (window 0); each test switches
it on with short windows.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.services import message_batcher
from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all, send_message
from tests.replay.test_router_v2 import (  # noqa: F401 — fixtures + helpers shared with the router suite
    _no_reply_delay, get_conv, messages, prime_conv, router, say, seed_shop,
)

# ruff: noqa: F811
pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

FRAGMENTS = ["I want", "to buy", "banarasi silk sadi", "if red colour is there then good"]
MERGED = "I want to buy banarasi silk sadi if red colour is there then good"


def ph(n: str) -> tuple[str, str]:
    """(phone, phone_number_id) unique per test."""
    return f"91950080{n}", f"788{n}"


@pytest.fixture(autouse=True)
def _fresh_batcher():
    """Batcher state is process-global."""
    message_batcher.reset_state()
    yield
    message_batcher.reset_state()


def enable_batching(monkeypatch, *, window=0.5, short=None, extended=0.9, max_wait=4.0, max_messages=8):
    """Turn batching on for the app's (patched) settings object, with test-sized windows."""
    from app.routers import webhook

    settings = webhook.get_settings()
    for name, value in (
        ("message_batch_window_seconds", window), ("message_batch_short_window_seconds", window if short is None else short),
        ("message_batch_extended_window_seconds", extended),
        ("message_batch_max_wait_seconds", max_wait), ("message_batch_max_messages", max_messages),
    ):
        monkeypatch.setattr(settings, name, value)


async def burst(http, phone, pnid, texts, gap=0.08, wamids=None):
    """Send the texts as separate webhook calls `gap` seconds apart (each an independent request)."""
    async def one(i, text):
        await asyncio.sleep(i * gap)
        return await send_message(http, phone, text, phone_number_id=pnid, wamid=(wamids[i] if wamids else None))

    return await asyncio.gather(*(one(i, t) for i, t in enumerate(texts)))


async def user_messages(session, client_id):
    """Stored customer messages of the client's conversations, oldest first."""
    from app.models.conversation import Conversation

    conv_ids = [c for (c,) in (await session.execute(select(Conversation.id).where(Conversation.client_id == client_id))).all()]
    rows = []
    for cid in conv_ids:
        rows += [m for m in await messages(session, cid) if m.role == "user"]
    return rows


async def test_the_four_fragment_example_gets_one_reply_from_the_merged_text(replay_http, replay_session, router, monkeypatch):
    """'I want' / 'to buy' / 'banarasi silk sadi' / 'if red colour is there then good' → ONE router call, ONE reply, ONE stored message."""
    phone, pnid = ph("0001")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    enable_batching(monkeypatch)
    router.when(MERGED, "faq", {"query": "anything"})

    responses = await burst(replay_http, phone, pnid, FRAGMENTS)

    assert all(r.status_code == 200 for r in responses)
    assert sorted(r.json()["status"] for r in responses).count("batched") == 3
    assert [c["message"] for c in router.calls] == [MERGED]
    assert len(captured) == 1 and "talk to someone" in captured[0]
    stored = await user_messages(replay_session, client.id)
    assert [m.content for m in stored] == [MERGED]


async def test_a_single_message_is_still_answered_normally_after_the_window(replay_http, replay_session, router, monkeypatch):
    """One complete message: one reply, text untouched (just a short quiet window first)."""
    phone, pnid = ph("0002")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    enable_batching(monkeypatch, window=0.3)
    router.when("do you have sarees", "faq", {"query": "x"})

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    reply = await say(replay_http, captured, phone, pnid, "do you have sarees")

    assert loop.time() - t0 >= 0.3 and "talk to someone" in reply
    assert [c["message"] for c in router.calls] == ["do you have sarees"]


async def test_two_separate_bursts_each_get_their_own_answer(replay_http, replay_session, router, monkeypatch):
    """Silence longer than the window ends a batch: the later message is a new turn with its own reply."""
    phone, pnid = ph("0003")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    enable_batching(monkeypatch, window=0.25, extended=0.25)
    router.when("show me sarees", "faq", {"query": "x"})
    router.when("what about kurtis", "faq", {"query": "y"})

    await say(replay_http, captured, phone, pnid, "show me sarees")
    await say(replay_http, captured, phone, pnid, "what about kurtis")

    assert [c["message"] for c in router.calls] == ["show me sarees", "what about kurtis"]
    assert len(captured) == 2


async def test_order_answers_are_never_batched(replay_http, replay_session, router, monkeypatch):
    """At an open order question each message is processed on its own, in order (nothing is merged or held)."""
    phone, pnid = ph("0004")
    capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042", pending_order_quantity=2)
    enable_batching(monkeypatch, window=1.5, extended=3.0)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await send_message(replay_http, phone, "Pink", phone_number_id=pnid)
    await send_message(replay_http, phone, "M", phone_number_id=pnid)

    assert loop.time() - t0 < 1.4                                   # no quiet window was applied
    assert not router.calls                                         # both were ₹0 fast paths
    conv = await get_conv(replay_session, conv_id)
    assert conv.selected_color == "Pink" and conv.selected_size == "M"
    assert [m.content for m in await user_messages(replay_session, client.id)] == ["Pink", "M"]


async def test_a_meta_retry_of_a_batched_message_is_ignored(replay_http, replay_session, router, monkeypatch):
    """Re-delivering a follower's wamid (after the batch ran) must not create a second turn."""
    phone, pnid = ph("0005")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    enable_batching(monkeypatch, window=0.3, extended=0.3)
    router.when("red saree dikhao please", "faq", {"query": "x"})
    wamids = ["wamid.batch.a", "wamid.batch.b"]

    await burst(replay_http, phone, pnid, ["red saree", "dikhao please"], wamids=wamids)
    assert len(captured) == 1
    retry = await send_message(replay_http, phone, "dikhao please", phone_number_id=pnid, wamid="wamid.batch.b")

    assert retry.status_code == 200 and len(captured) == 1 and len(router.calls) == 1


async def test_batching_off_keeps_the_old_one_reply_per_message_behaviour(replay_http, replay_session, router, monkeypatch):
    """Window 0 (the suite default): every message is processed immediately and separately, exactly as before."""
    phone, pnid = ph("0006")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("show me sarees", "faq", {"query": "x"})
    router.when("and kurtis", "faq", {"query": "y"})

    await say(replay_http, captured, phone, pnid, "show me sarees")
    await say(replay_http, captured, phone, pnid, "and kurtis")

    assert [c["message"] for c in router.calls] == ["show me sarees", "and kurtis"] and len(captured) == 2


async def test_fragments_after_a_product_list_with_an_open_menu_get_one_reply(replay_http, replay_session, router, monkeypatch):
    """The reported case: a 'reply with the number' menu is open and the customer sends 'Pan' / 'mare bija' / 'choli jova che'."""
    phone, pnid = ph("0007")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                     pending_choice_skus='["SR20001", "SR20003"]')
    enable_batching(monkeypatch, window=0.4, short=0.6, extended=0.8)
    merged = "Pan mare bija choli jova che"
    router.when(merged, "faq", {"query": "x"}, language="gu")

    responses = await burst(replay_http, phone, pnid, ["Pan", "mare bija", "choli jova che"], gap=0.3)

    assert sorted(r.json()["status"] for r in responses).count("batched") == 2
    assert [c["message"] for c in router.calls] == [merged] and len(captured) == 1
    assert [m.content for m in await user_messages(replay_session, client.id)] == [merged]


async def test_a_menu_pick_while_a_menu_is_open_is_not_merged_or_held(replay_http, replay_session, router, monkeypatch):
    """'2' at an open menu is processed on its own immediately (a ₹0 fast path), never joined to word fragments."""
    phone, pnid = ph("0008")
    capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                               pending_choice_skus='["SR20001", "SR20003"]')
    enable_batching(monkeypatch, window=1.5, short=2.0, extended=3.0)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await send_message(replay_http, phone, "2", phone_number_id=pnid)

    assert loop.time() - t0 < 1.4 and not router.calls
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "SR20003"
