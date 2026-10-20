"""
Replay tests for the out-of-scope boundary (real webhook → pipeline → Postgres, scripted LLM).

Covers both front doors: the LLM router (`out_of_scope` action → fixed template, shop profile in the prompt,
never-block guards, strike counter, mid-order re-ask) and the keyword pipeline's off-topic check (profile
keyword pre-check, shop context in the classifier prompt). Also the shop profile itself: built on first use,
rebuilt when the catalogue changes, untouched by price/stock edits, invalidated by KB edits.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.services import conversation_flow as _conversation_flow
from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all
from tests.replay.test_router_v2 import (  # noqa: F401 — fixtures + helpers shared with the router suite
    POISON, ScriptedRouter, _no_reply_delay, get_conv, messages, prime_conv, router, say, seed_shop,
)

# ruff: noqa: F811 — `router` is a fixture imported from the router suite and used as a test argument
pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

_REAL_IS_OFF_TOPIC = _conversation_flow.is_off_topic_message      # the replay fixture swaps the module attribute
WEATHER = "Ahmedabad ma su vatavaran chr"


def ph(n: str) -> tuple[str, str]:
    """(phone, phone_number_id) unique per test (own number range, away from the router suite's)."""
    return f"91950070{n}", f"787{n}"


async def get_client(session, client_id: int):
    """Fresh Client row."""
    from app.models.client import Client

    r = await session.execute(select(Client).where(Client.id == client_id).execution_options(populate_existing=True))
    return r.scalar_one()


# ── router: the boundary ─────────────────────────────────────────────────────

async def test_weather_question_gets_the_fixed_boundary_template_not_model_text(replay_http, replay_session, router, monkeypatch):
    """The screenshot case: out_of_scope → fixed Roman-Gujarati template; the model's hint (poison) never reaches the customer."""
    phone, pnid = ph("0001")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when(WEATHER, "out_of_scope", language="gu", reply_hint=f"{POISON} Ahmedabad ma mausam garam chhe")

    reply = await say(replay_http, captured, phone, pnid, WEATHER)

    assert reply.startswith("Hu fakt Router Test Store ma madad kari shaku chhu"), reply
    assert "router-0001" in reply and POISON not in reply and "mausam" not in reply
    conv = (await _only_conv(replay_session, client.id))
    assert conv.off_topic_count == 1 and conv.current_stage in ("greeting", "product_inquiry")
    stored = [m.content for m in await messages(replay_session, conv.id)]
    assert stored[0] == WEATHER and stored[-1] == reply


async def _only_conv(session, client_id: int):
    """The client's single conversation."""
    from app.models.conversation import Conversation

    r = await session.execute(select(Conversation).where(Conversation.client_id == client_id)
                              .execution_options(populate_existing=True))
    return r.scalars().one()


async def test_router_prompt_carries_the_shop_profile_and_it_is_persisted(replay_http, replay_session, router, monkeypatch):
    """The router sees what this shop sells (type-less shop: categories + product names); the profile is saved on the client."""
    phone, pnid = ph("0002")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("tell me a joke", "out_of_scope", confidence=0.9)

    await say(replay_http, captured, phone, pnid, "tell me a joke")

    prompt = router.calls[-1]["user"]
    assert "what the business offers:" in prompt
    categories = prompt.split("categories: ", 1)[1].split(" | ")[0].split(", ")
    assert sorted(categories) == ["Kurti", "Lehenga", "Saree"]
    for part in ("Cotton Lehenga", "Printed Kurti"):
        assert part in prompt, part
    row = await get_client(replay_session, client.id)
    assert row.shop_context and "Saree" in row.shop_context
    assert row.shop_context_hash and row.shop_context_updated_at is not None
    assert "saree" in row.shop_context_keywords and "kurti" in row.shop_context_keywords


async def test_low_confidence_out_of_scope_becomes_a_clarifying_question(replay_http, replay_session, router, monkeypatch):
    """A shaky out_of_scope (<0.7) is never a refusal: the customer gets the normal clarify reply and no strike."""
    phone, pnid = ph("0003")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("hmm kuch alag puchna tha", "out_of_scope", confidence=0.6, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "hmm kuch alag puchna tha")

    assert "Main sirf" not in reply and "I can only help" not in reply
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 0


async def test_out_of_scope_on_a_message_with_a_shop_word_is_not_refused(replay_http, replay_session, router, monkeypatch):
    """Even a confident out_of_scope is overridden when the message uses the shop's own vocabulary ('saree')."""
    phone, pnid = ph("0004")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("barish me saree kaisi rahegi", "out_of_scope", confidence=0.95, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "barish me saree kaisi rahegi")

    assert "I can only help" not in reply and "Main sirf" not in reply
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 0


async def test_third_strike_is_the_final_notice_and_an_on_topic_message_resets_the_streak(replay_http, replay_session, router, monkeypatch):
    """Strikes 1–2 = boundary + catalogue link; a real request resets; the next 3 in a row end in the final notice + mute."""
    monkeypatch.setattr("app.routers.webhook._RATE_LIMIT_MESSAGES", 1000)     # 6 quick messages; not under test
    phone, pnid = ph("0005")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    for text in ("who won the match", "what is the capital of france", "tell me a joke", "weather today", "what is 2+2"):
        router.when(text, "out_of_scope", confidence=0.95)
    router.when("show me kurtis", "search_catalog", {"query": "kurti"})

    first_two = [await say(replay_http, captured, phone, pnid, t) for t in ("who won the match", "what is the capital of france")]
    assert all("I can only help with Router Test Store" in r and "router-0005" in r for r in first_two)
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 2

    await say(replay_http, captured, phone, pnid, "show me kurtis")
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 0       # on-topic → streak resets

    later = [await say(replay_http, captured, phone, pnid, t) for t in ("tell me a joke", "weather today", "what is 2+2")]
    assert not any("pause replies" in r for r in later[:2]) and "pause replies for about 15 minutes" in later[2]
    assert "router-0005" in later[2]                                                # the final notice carries the catalogue too
    conv = await _only_conv(replay_session, client.id)
    assert conv.off_topic_count == 3 and conv.spam_muted_until is not None and conv.ai_enabled is not False


async def test_mid_order_off_topic_reasks_the_open_question_and_changes_no_state(replay_http, replay_session, router, monkeypatch):
    """At the colour question: boundary prefix + the SAME colour question again; slots/stage untouched."""
    phone, pnid = ph("0006")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042", pending_order_quantity=2)
    router.when("what is the weather today", "out_of_scope", confidence=0.95)

    reply = await say(replay_http, captured, phone, pnid, "what is the weather today")

    assert reply.startswith("I can only help with your order and Router Test Store")
    assert "Pink" in reply and "Blue" in reply                      # the colour options are offered again
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "order_collection" and conv.selected_color is None
    assert conv.pending_order_quantity == 2 and conv.pending_product_sku == "LH10042" and conv.off_topic_count == 1


async def test_an_address_misread_as_out_of_scope_is_still_taken_as_the_answer(replay_http, replay_session, router, monkeypatch):
    """At the address question a 0.8-confidence out_of_scope is not trusted: the text goes to the slot machine as the address."""
    phone, pnid = ph("0013")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    address = "12 Weather Office Road, Ahmedabad 380001"
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="LH10042",
        last_shown_sku="LH10042", pending_order_quantity=1, selected_color="Pink", selected_size="M",
        customer_name="Asha Shah",
    )
    router.when(address, "out_of_scope", confidence=0.8)

    reply = await say(replay_http, captured, phone, pnid, address)

    assert "I can only help" not in reply
    conv = await get_conv(replay_session, conv_id)
    assert conv.delivery_address == address and conv.off_topic_count == 0


async def test_shop_advice_is_still_answered_for_a_shop_that_sells_it(replay_http, replay_session, router, monkeypatch):
    """general_answer for the shop's own category keeps working exactly as before (guarded model hint)."""
    phone, pnid = ph("0007")
    captured = capture_all(monkeypatch)
    await seed_shop(replay_session, phone, pnid)
    router.when("silk saree kaise dhoye", "general_answer", language="hinglish",
                reply_hint="Silk ko halke detergent se haath se dhoyein aur chhaon mein sukhayein.")

    reply = await say(replay_http, captured, phone, pnid, "silk saree kaise dhoye")

    assert reply == "Silk ko halke detergent se haath se dhoyein aur chhaon mein sukhayein."


async def test_button_taps_and_exact_skus_skip_the_router_and_clear_a_strike(replay_http, replay_session, router, monkeypatch):
    """A fast-path message (exact SKU) never calls the router and ends an off-topic streak."""
    phone, pnid = ph("0008")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("tell me a joke", "out_of_scope", confidence=0.95)
    await say(replay_http, captured, phone, pnid, "tell me a joke")
    calls = len(router.calls)

    await say(replay_http, captured, phone, pnid, "LH10042")

    assert len(router.calls) == calls
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 0


# ── search miss: not sold / out of stock ─────────────────────────────────────

async def test_item_the_shop_does_not_sell_is_unavailable_with_the_catalogue_not_unrelated_products(replay_http, replay_session, router, monkeypatch):
    """The 'Shoes available?' screenshot: 'not available' + catalogue link; no kurtis/sarees suggested, no pick menu opened."""
    phone, pnid = ph("0014")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("Shoes available?", "search_catalog", {"query": "shoes"})

    reply = await say(replay_http, captured, phone, pnid, "Shoes available?")

    assert reply.startswith("Sorry, that item is currently not available in our store.")
    assert "router-0014" in reply and "shoes" not in reply.lower()
    for unrelated in ("Saree", "Kurti", "Lehenga", "[", "₹"):
        assert unrelated not in reply.replace("Router Test Store", ""), unrelated
    assert not (await _only_conv(replay_session, client.id)).pending_choice_skus


async def test_sold_out_product_is_reported_as_out_of_stock_with_the_catalogue_link(replay_http, replay_session, router, monkeypatch):
    """The only kurti is sold out: 'Printed Kurti is currently out of stock' + catalogue link, nothing unrelated suggested."""
    from app.models.product import Product

    phone, pnid = ph("0015")
    captured = capture_all(monkeypatch)
    client, _, kurti = await seed_shop(replay_session, phone, pnid)
    row = (await replay_session.execute(select(Product).where(Product.id == kurti.id))).scalar_one()
    row.stock = 0
    await replay_session.commit()
    router.when("is kurti available", "search_catalog", {"query": "kurti"})

    reply = await say(replay_http, captured, phone, pnid, "is kurti available")

    assert reply.startswith("Sorry, Printed Kurti is currently out of stock.")
    assert "router-0015" in reply and "Saree" not in reply and "Lehenga" not in reply


async def test_sold_out_product_with_similar_items_lists_only_relevant_ones(replay_http, replay_session, router, monkeypatch):
    """Red Silk Saree sold out: say so, then offer the OTHER sarees (same kind) — never kurtis/lehengas."""
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    phone, pnid = ph("0016")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    red = (await replay_session.execute(select(Product).where(Product.client_id == client.id, Product.sku == "SR20002"))).scalar_one()
    for v in (await replay_session.execute(select(ProductVariant).where(ProductVariant.product_id == red.id))).scalars():
        v.stock = 0
    await replay_session.commit()
    router.when("is red silk saree available", "search_catalog", {"query": "red silk saree"})

    reply = await say(replay_http, captured, phone, pnid, "is red silk saree available")

    assert reply.startswith("Sorry, Red Silk Saree is currently out of stock.")
    assert "closest options" in reply and "[SR20001]" in reply and "[SR20003]" in reply
    assert "SR20002" not in reply and "Kurti" not in reply and "Lehenga" not in reply


# ── shop profile lifecycle ───────────────────────────────────────────────────

async def test_catalogue_edit_rebuilds_the_profile_but_price_and_stock_edits_do_not(replay_http, replay_session, router, monkeypatch):
    """Add a product → next message's prompt lists it; change price/stock → profile row untouched."""
    from app.models.product import Product

    phone, pnid = ph("0009")
    captured = capture_all(monkeypatch)
    client, _, kurti = await seed_shop(replay_session, phone, pnid)
    for text in ("joke one", "joke two", "joke three"):
        router.when(text, "out_of_scope", confidence=0.95)

    await say(replay_http, captured, phone, pnid, "joke one")
    first = await get_client(replay_session, client.id)
    assert "Jacket" not in first.shop_context
    stamp = first.shop_context_updated_at

    kurti_row = (await replay_session.execute(select(Product).where(Product.id == kurti.id))).scalar_one()
    kurti_row.price, kurti_row.stock = 999.0, 0
    await replay_session.commit()
    await say(replay_http, captured, phone, pnid, "joke two")
    assert (await get_client(replay_session, client.id)).shop_context_updated_at == stamp

    replay_session.add(Product(client_id=client.id, name="Denim Jacket", sku="JK1", price=1500.0, stock=3,
                               is_active=True, category="Jacket"))
    await replay_session.commit()
    await say(replay_http, captured, phone, pnid, "joke three")
    rebuilt = await get_client(replay_session, client.id)
    assert "Jacket" in rebuilt.shop_context and "Denim Jacket" in router.calls[-1]["user"]
    assert rebuilt.shop_context_updated_at > stamp


async def test_kb_topics_join_the_profile_and_invalidate_forces_a_rebuild(replay_http, replay_session, router, monkeypatch):
    """Approved KB question titles appear under 'shop topics'; invalidate() clears the hash so the next turn rebuilds."""
    from app.models.knowledge_base import KnowledgeBase
    from app.services import shop_context_service

    phone, pnid = ph("0010")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    replay_session.add(KnowledgeBase(client_id=client.id, question="Return policy", answer="7 days", is_active=True,
                                     is_approved=True))
    replay_session.add(KnowledgeBase(client_id=client.id, question="Secret draft topic", answer="x", is_active=True,
                                     is_approved=False))
    await replay_session.commit()
    router.when("joke one", "out_of_scope", confidence=0.95)
    router.when("joke two", "out_of_scope", confidence=0.95)

    await say(replay_http, captured, phone, pnid, "joke one")
    row = await get_client(replay_session, client.id)
    assert "shop topics: Return policy" in row.shop_context and "Secret draft topic" not in row.shop_context

    await shop_context_service.invalidate(replay_session, client.id)
    assert (await get_client(replay_session, client.id)).shop_context_hash is None
    await say(replay_http, captured, phone, pnid, "joke two")
    assert (await get_client(replay_session, client.id)).shop_context_hash is not None


# ── keyword pipeline (router OFF) ────────────────────────────────────────────

def _stub_off_topic_llm(monkeypatch, *, verdict: str):
    """Use the REAL is_off_topic_message with a stubbed classifier call; returns the call recorder."""
    monkeypatch.setattr("app.services.conversation_flow.is_off_topic_message", _REAL_IS_OFF_TOPIC)
    chat = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=verdict))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1),
    ))
    monkeypatch.setattr("app.services.llm_client.chat", chat)
    return chat


async def test_keyword_path_deflects_weather_using_the_shop_context(replay_http, replay_session, monkeypatch):
    """Router OFF: the idle off-topic check gets the shop profile and the weather question is deflected with the template."""
    phone, pnid = ph("0011")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid, router_on=False)
    chat = _stub_off_topic_llm(monkeypatch, verdict="YES")

    reply = await say(replay_http, captured, phone, pnid, WEATHER)

    # same fixed message + catalogue link as the router, in the customer's language (Roman Gujarati here)
    assert reply.startswith("Hu fakt Router Test Store ma madad kari shaku chhu") and "router-0011" in reply
    prompt = chat.call_args.args[1][0]["content"]
    assert "What the business offers:" in prompt and "Cotton Lehenga" in prompt and WEATHER in prompt
    assert (await _only_conv(replay_session, client.id)).off_topic_count == 1


async def test_keyword_path_shop_word_message_skips_the_classifier(replay_http, replay_session, monkeypatch):
    """Router OFF: a message that uses the shop's vocabulary is on-topic with ZERO classifier calls."""
    phone, pnid = ph("0012")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid, router_on=False)
    chat = _stub_off_topic_llm(monkeypatch, verdict="YES")           # would deflect if it were ever asked

    reply = await say(replay_http, captured, phone, pnid, "kya aap silk saree dhone ka tarika bata sakte ho")

    chat.assert_not_called()
    assert "I can only help" not in reply
