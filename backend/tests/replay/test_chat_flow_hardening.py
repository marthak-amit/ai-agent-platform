"""
Regression replays for the chat-flow problems seen in the real conv=60 (Riya Sarees) transcript of 2026-10-17.

Every test drives real webhook POSTs through the ASGI app into Postgres with a scripted router LLM, and asserts the
reply text, the buttons and the conversation row:

  P0-1  a router `cancel_order` never wipes a draft without explicit cancel wording + a very sure model ("Cancel this order?")
  P0-2  "sorry i cant get it!" / "what are you trying to say" / "?" -> `confused`: nothing is cancelled, the question is re-asked
  P0-3  small talk never answers with a bare "Sure, let's continue!"; "continue" / "yes" restores a just-cancelled draft
  P1-1  quantity change with the colour already chosen -> summary again (no "Same for all 2?"); "different" asks which colours
  P1-2  "show me some more products like that" -> a list WITHOUT the product already in play; "another one?" continues it
  P1-3  another product mid-order -> "Switch / Add both / Keep current" (never a silent switch)
  P1-4  "please" after a discount refusal is a discount follow-up (not start_order); the log shows what the customer typed
  P2    fuzzy "Did you mean" only for real matches; summary text == buttons; "UPI only" once, in the summary line;
        "20 Oct" not "20 Oct – 20 Oct"; one question after a discount; high-value payments need a manual review

`fixtures/conv60_2026_10_17.json` replays the real customer turns (router verdicts as the live model returned them).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.replay.conftest import _PG_AVAILABLE, replay_db_url, replay_http, replay_session  # noqa: F401
from tests.replay.test_router_v2 import get_conv, prime_conv
from tests.test_conv60_regression import (  # noqa: F401  (fixtures are used by name)
    Outbox,
    _clean_replay_db,
    _no_reply_delay,
    _reset_llm_health,
    body,
    one_reply,
    outbox,
    router,
    turn,
)

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

FIXTURES = Path(__file__).parent / "fixtures"


def _ids(n: int) -> tuple[str, str]:
    """(customer phone, phone_number_id) unique per test."""
    return f"9195100{n:05d}", f"8885{n:05d}"


# ── seeding ──────────────────────────────────────────────────────────────────

async def seed_shop(session, phone: str, pnid: str, *, accepts_cod: bool = False):
    """
    'Riya Sarees' (UPI-only unless accepts_cod), router on:
      JW10944 golden necklace ₹150,000 (Antique / Rose Gold, 2 business days) + 3 cheaper jewellery items
      SR63896 Paisley Georgette Saree ₹5,599 (Green / Grey / Orange / Red) + 3 cheaper sarees
      DP91093 Gota Patti Banarasi Dupatta, SR38567 Gota Patti Banarasi Silk Saree (the 'Gota' fuzzy-match trap)
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Riya Sarees Surat", email=f"flow_{phone}@test.com", phone=phone[:-1] + "0",
        whatsapp_phone_number_id=pnid, accepts_cod=accepts_cod, accepts_upi=True, upi_id="riyasarees@paytm",
        hashed_password="x", delivery_days_min=3, delivery_days_max=7, router_v2_enabled=True, catalogue_slug="riyasarees",
    )
    session.add(client)
    await session.flush()
    rows = [
        # sku, name, price, category, variants?, delivery_days
        ("JW10944", "golden necklace", 150000.0, "Jewellery", ["Antique", "Rose Gold"], 2),
        ("JW10950", "Pearl Necklace", 2500.0, "Jewellery", None, None),
        ("JW10960", "Kundan Choker", 3200.0, "Jewellery", None, None),
        ("JW10970", "Temple Jewellery Set", 4100.0, "Jewellery", None, None),
        ("SR63896", "Paisley Georgette Saree", 5599.0, "Saree", ["Green", "Grey", "Orange", "Red"], None),
        ("SR27754", "Banarasi Silk Saree", 2450.0, "Saree", None, None),
        ("SR33210", "Cotton Printed Saree", 850.0, "Saree", None, None),
        ("SR29821", "Kanjivaram Silk Saree", 4200.0, "Saree", None, None),
        ("DP91093", "Gota Patti Banarasi Dupatta", 1549.0, "Dupatta", None, None),
        ("SR38567", "Gota Patti Banarasi Silk Saree", 6899.0, "Saree", None, None),
    ]
    prods = {}
    for sku, name, price, category, colours, days in rows:
        p = Product(
            client_id=client.id, name=name, sku=sku, price=price, stock=0 if colours else 10, is_active=True,
            has_variants=bool(colours), category=category, delivery_days=days,
        )
        session.add(p)
        prods[sku] = p
    await session.flush()
    for sku, name, price, category, colours, days in rows:
        for colour in colours or []:
            session.add(ProductVariant(
                product_id=prods[sku].id, client_id=client.id, color=colour, size=None, stock=10, is_active=True, price=price,
            ))
    await session.commit()
    await session.refresh(client)
    return client


async def seed_paid_order(session, client, conv_id: int, phone: str, *, sku="JW10944", name="golden necklace", total=150000.0):
    """A finished (paid) order, so 'that' / 'last ordered' has a referent."""
    from app.models.order import Order
    from app.models.product import Product

    product_id = (await session.execute(select(Product.id).where(Product.client_id == client.id, Product.sku == sku))).scalar_one()
    now = datetime.now(timezone.utc)
    order = Order(
        order_number="ORD-2026-0067", client_id=client.id, conversation_id=conv_id, customer_name="Amit", product_id=product_id,
        customer_phone=phone, delivery_address="702 Somerest, Ahmedabad", product_name=name, product_sku=sku, quantity=1,
        unit_price=total, total_amount=total, payment_method="UPI", status="paid", paid_at=now, confirmed_at=now,
        created_at=now,
    )
    session.add(order)
    await session.commit()
    return order


async def seed_conv(session, client, phone, *, stage="greeting", **fields) -> int:
    """A conversation for Amit (name + address known)."""
    fields.setdefault("customer_name", "Amit")
    fields.setdefault("delivery_address", "702 Somerest, Ahmedabad")
    return await prime_conv(session, phone=phone, client_id=client.id, stage=stage, **fields)


def buttons_of(events: list[tuple]) -> list[str]:
    """Titles of the buttons / list rows of the last interactive message of a turn."""
    for kind, _to, _text, extra in reversed(events):
        if kind in ("buttons", "list"):
            return list(extra)
    return []


async def tap(http, box, phone, pnid, button_id: str, title: str) -> list[tuple]:
    """Tap a (raw-id) button as the customer would; returns the bot's events for that tap."""
    import time as _time

    from tests.replay.helpers import send_button

    n = len(box.events)
    resp = await send_button(http, phone, button_id, title, wamid=f"wamid.flow.{_time.time_ns()}", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    return box.since(n, phone)


# ═════════════════════════════════════════════════════════════════════════════
# P0-1  cancel safety
# ═════════════════════════════════════════════════════════════════════════════

async def _split_state(session, client, phone):
    """order_collection at 'How many of Red?' — the exact state the real customer was in."""
    return await seed_conv(
        session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
        pending_order_quantity=2, cart_variant_mode="different", cart_collection_mode="loop", payment_method="UPI",
    )


async def test_cant_get_it_is_never_a_cancel_even_when_the_model_says_cancel(replay_http, replay_session, router, outbox):
    """'sorry i cant get it!' + router cancel_order(0.9) -> NO cancel: the draft survives, the question comes back simpler."""
    phone, pnid = _ids(1)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _split_state(replay_session, client, phone)
    router.when("sorry i cant get it!", "cancel_order", confidence=0.9)

    ev = await turn(replay_http, outbox, "sorry i cant get it!", phone=phone, pnid=pnid)

    one_reply(ev)
    text = body(ev)
    assert "Order cancelled" not in text and "cancel" not in text.lower().replace("cancel this order", "")
    assert "Sorry for the confusion" in text
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.selected_color == "Red" and conv.pending_order_quantity == 2
    assert conv.last_cancelled_draft is None and conv.cancel_confirm_at is None
    assert conv.current_stage in ("order_collection", "awaiting_final_confirmation")


async def test_a_vague_cancel_asks_yes_no_first_and_yes_cancels_no_keeps(replay_http, replay_session, router, outbox):
    """cancel_order without explicit cancel wording -> 'Cancel this order? (Yes / No)' + buttons; the stage does not move."""
    phone, pnid = _ids(2)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
    )
    router.when("i am done with this", "cancel_order", confidence=0.99)
    router.when("hmm this is not working", "cancel_order", confidence=0.9)

    ev = await turn(replay_http, outbox, "i am done with this", phone=phone, pnid=pnid)       # 0.99 but no cancel word
    assert body(ev) == "Cancel this order? (Yes / No)"
    assert buttons_of(ev) == ["Yes, cancel", "No, keep it"]
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "order_collection" and conv.pending_product_sku == "SR63896"
    assert conv.cancel_confirm_at is not None and conv.last_cancelled_draft is None

    ev = await tap(replay_http, outbox, phone, pnid, "cancel_no", "No, keep it")               # No -> nothing cancelled
    assert "still on" in body(ev) and "Quantity?" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.cancel_confirm_at is None

    await turn(replay_http, outbox, "hmm this is not working", phone=phone, pnid=pnid)
    ev = await turn(replay_http, outbox, "yes", phone=phone, pnid=pnid)                         # Yes -> cancelled + snapshot
    assert "Order cancelled" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku is None and conv.current_stage == "greeting" and conv.cancel_confirm_at is None
    assert conv.last_cancelled_draft["fields"]["pending_product_sku"] == "SR63896"
    assert conv.last_cancelled_draft["fields"]["selected_color"] == "Red"


async def test_explicit_cancel_with_a_sure_model_cancels_directly_and_saves_the_draft(replay_http, replay_session, router, outbox):
    """'cancel my order please' + 0.97 -> cancelled at once (no extra question) and the draft snapshot (slots + ts) is stored."""
    phone, pnid = _ids(3)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
        pending_order_quantity=2, cart_variant_mode="same",
    )
    router.when("cancel my order please", "cancel_order", confidence=0.97)

    ev = await turn(replay_http, outbox, "cancel my order please", phone=phone, pnid=pnid)

    assert "Order cancelled" in body(ev) and "(Yes / No)" not in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku is None and conv.selected_color is None
    snap = conv.last_cancelled_draft
    assert snap["fields"]["pending_product_sku"] == "SR63896" and snap["fields"]["pending_order_quantity"] == 2
    assert snap["fields"]["cart_variant_mode"] == "same"
    assert datetime.fromisoformat(snap["ts"]) > datetime.now(timezone.utc) - timedelta(minutes=1)


async def test_explicit_cancel_word_but_an_unsure_model_still_asks(replay_http, replay_session, router, outbox):
    """Explicit wording is not enough on its own: confidence below 0.95 -> the yes/no question."""
    phone, pnid = _ids(4)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="awaiting_final_confirmation", pending_product_sku="SR63896",
        selected_color="Red", pending_order_quantity=1, payment_method="UPI", summary_shown=True,
    )
    router.when("please cancel this order maybe", "cancel_order", confidence=0.8)

    ev = await turn(replay_http, outbox, "please cancel this order maybe", phone=phone, pnid=pnid)

    assert body(ev) == "Cancel this order? (Yes / No)"
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "SR63896"


# ═════════════════════════════════════════════════════════════════════════════
# P0-2  confusion
# ═════════════════════════════════════════════════════════════════════════════

async def test_confused_aborts_the_colour_split_and_falls_back_to_the_single_variant_path(replay_http, replay_session, router, outbox):
    """'what are you trying to say' at 'which colours / how many' -> split dropped (mode 'same'), summary back, no state loss."""
    phone, pnid = _ids(5)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _split_state(replay_session, client, phone)
    router.when("what are you trying to say", "confused", confidence=0.9)

    ev = await turn(replay_http, outbox, "what are you trying to say", phone=phone, pnid=pnid)

    text = body(ev)
    assert "keep it simple" in text and "Order Summary" in text and "Paisley Georgette Saree Red × 2" in text
    conv = await get_conv(replay_session, conv_id)
    assert conv.cart_variant_mode == "same" and not conv.cart_items and conv.cart_wip_item is None
    assert conv.selected_color == "Red" and conv.pending_order_quantity == 2 and conv.pending_product_sku == "SR63896"


@pytest.mark.parametrize("message,question", [
    ("samjh nahi aaya", "Kitne pieces chahiye?"), ("kya bol rahe ho", "Kitne pieces chahiye?"), ("?", "Quantity?"),
    ("confusing", "Quantity?"), ("I can't understand", "Quantity?"),
])
async def test_confusion_phrases_are_handled_without_the_llm_and_change_nothing(replay_http, replay_session, router, outbox, message, question):
    """The deterministic confusion lexicon (and a bare '?') answers at ₹0: the router is never called, the draft is intact."""
    phone, pnid = _ids(6)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
    )

    ev = await turn(replay_http, outbox, message, phone=phone, pnid=pnid)

    one_reply(ev)
    assert question in body(ev) and router.calls == []
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.selected_color == "Red" and conv.current_stage == "order_collection"


async def test_confused_with_no_open_question_sends_the_menu(replay_http, replay_session, router, outbox):
    """No order / offer open -> a friendly 'what can I do' menu with the catalogue link, nothing else."""
    phone, pnid = _ids(7)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="greeting")

    ev = await turn(replay_http, outbox, "what do you mean", phone=phone, pnid=pnid)

    assert "Sorry for the confusion" in body(ev) and "riyasarees" in body(ev)


# ═════════════════════════════════════════════════════════════════════════════
# P0-3  no empty small talk / restore
# ═════════════════════════════════════════════════════════════════════════════

async def _cancel_then(http, session, box, router, phone, pnid, client):
    """Cancel an order in progress explicitly; returns the conversation id."""
    conv_id = await seed_conv(
        session, client, phone, stage="awaiting_final_confirmation", pending_product_sku="SR63896", selected_color="Red",
        pending_order_quantity=2, cart_variant_mode="same", payment_method="UPI", summary_shown=True,
    )
    router.when("cancel", "cancel_order", confidence=0.99)
    await turn(http, box, "cancel", phone=phone, pnid=pnid)
    return conv_id


@pytest.mark.parametrize("message", ["I want to continue this", "yes", "ye", "haan", "continue"])
async def test_continue_after_a_cancel_restores_the_draft(replay_http, replay_session, router, outbox, message):
    """'I want to continue this' / 'yes' / 'ye' right after a cancel -> 'Restored your order: …' and the next step, no LLM."""
    phone, pnid = _ids(8)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _cancel_then(replay_http, replay_session, outbox, router, phone, pnid, client)
    assert (await get_conv(replay_session, conv_id)).pending_product_sku is None
    calls_before = len(router.calls)

    ev = await turn(replay_http, outbox, message, phone=phone, pnid=pnid)

    text = body(ev)
    assert text.startswith("Restored your order: Paisley Georgette Saree Red × 2")
    assert "Order Summary" in text and "Sure, let's" not in text
    assert len(router.calls) == calls_before                                   # ₹0: deterministic
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.selected_color == "Red" and conv.pending_order_quantity == 2
    assert conv.current_stage == "awaiting_final_confirmation" and conv.last_cancelled_draft is None


async def test_the_restore_window_is_thirty_minutes(replay_http, replay_session, router, outbox):
    """A snapshot older than the TTL is not restored; the customer gets a real answer, not a generic ack."""
    phone, pnid = _ids(9)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _cancel_then(replay_http, replay_session, outbox, router, phone, pnid, client)
    conv = await get_conv(replay_session, conv_id)
    old = dict(conv.last_cancelled_draft)
    old["ts"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    conv.last_cancelled_draft = old
    await replay_session.commit()
    router.when("i want to continue this", "smalltalk", confidence=0.9, reply_hint="Sure, let's continue!")

    ev = await turn(replay_http, outbox, "I want to continue this", phone=phone, pnid=pnid)

    text = body(ev)
    assert "Restored" not in text and "Sure, let's continue!" not in text
    assert "Browse here" in text                                              # menu fallback instead of a bare ack
    assert (await get_conv(replay_session, conv_id)).pending_product_sku is None


async def test_smalltalk_never_buries_an_open_question_or_sends_a_bare_ack(replay_http, replay_session, router, outbox):
    """With a product on offer, small talk 'Sure, let's proceed!' is replaced by the question that is waiting."""
    phone, pnid = _ids(10)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="product_inquiry", pending_product_sku="SR33210")
    router.when("hmm let me see", "smalltalk", confidence=0.9, reply_hint="Sure, let's proceed!")

    ev = await turn(replay_http, outbox, "hmm let me see", phone=phone, pnid=pnid)

    assert body(ev) == "Would you like to order Cotton Printed Saree [SR33210]? (Yes / No)"


async def test_pure_ack_hints_are_never_sent_but_thanks_gets_a_friendly_reply(replay_http, replay_session, router, outbox):
    """No context + a <6-word ack hint -> the menu; a real 'thanks' keeps its short friendly reply (₹0, side-question guard)."""
    phone, pnid = _ids(11)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="greeting")
    router.when("mm hmm interesting", "smalltalk", confidence=0.9, reply_hint="Sure thing!")

    ev = await turn(replay_http, outbox, "mm hmm interesting", phone=phone, pnid=pnid)
    assert "Sure thing" not in body(ev) and "Browse here" in body(ev)
    ev = await turn(replay_http, outbox, "thanks a lot", phone=phone, pnid=pnid)
    assert body(ev) == "You're welcome! 😊"


# ═════════════════════════════════════════════════════════════════════════════
# P1-1  quantity change
# ═════════════════════════════════════════════════════════════════════════════

async def test_quantity_change_keeps_the_colour_and_returns_to_the_summary(replay_http, replay_session, router, outbox):
    """conv=60: at the summary 'sorry i want 2 quantity' -> Red kept, qty 2, summary again — not 'Same for all 2?'."""
    phone, pnid = _ids(12)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="awaiting_final_confirmation", pending_product_sku="SR63896",
        selected_color="Red", pending_order_quantity=1, payment_method="UPI", summary_shown=True,
    )
    router.when("sorry i want 2 quantity", "change_slot", {"slot": "quantity", "value": "2"}, confidence=0.97)

    ev = await turn(replay_http, outbox, "sorry i want 2 quantity", phone=phone, pnid=pnid)

    text = body(ev)
    assert "Updated ✅ Quantity: 2" in text and "Paisley Georgette Saree Red × 2 = ₹11,198" in text
    assert "Same for all" not in text and "different for each" not in text and "How many of" not in text
    conv = await get_conv(replay_session, conv_id)
    assert conv.selected_color == "Red" and conv.pending_order_quantity == 2 and conv.cart_variant_mode == "same"
    assert conv.current_stage == "awaiting_final_confirmation" and not conv.cart_items


async def test_different_colours_asks_which_colours_first_never_how_many_of_red(replay_http, replay_session, router, outbox):
    """'different' at the split question -> 'Which colours …, and how many of each?'; '1 Red, 1 Grey' commits both lines."""
    phone, pnid = _ids(13)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
        pending_order_quantity=2, payment_method="UPI",
    )

    ev = await turn(replay_http, outbox, "different", phone=phone, pnid=pnid)
    text = body(ev)
    assert "Which colours would you like for your 2 pieces" in text and "How many of Red" not in text

    ev = await turn(replay_http, outbox, "1 Red, 1 Grey", phone=phone, pnid=pnid)
    conv = await get_conv(replay_session, conv_id)
    assert sorted((i["color"], i["qty"]) for i in conv.cart_items) == [("Grey", 1), ("Red", 1)]
    assert "Order Summary" in body(ev)


# ═════════════════════════════════════════════════════════════════════════════
# P1-2  search vs sticky SKU
# ═════════════════════════════════════════════════════════════════════════════

async def test_more_products_like_that_lists_other_items_never_the_ordered_one(replay_http, replay_session, router, outbox):
    """conv=60 ('show me some more products like that' re-sent the necklace card) -> a list without JW10944, no LLM."""
    phone, pnid = _ids(14)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="greeting")
    await seed_paid_order(replay_session, client, conv_id, phone)

    ev = await turn(replay_http, outbox, "show me some more products like that", phone=phone, pnid=pnid)

    text = body(ev)
    assert "JW10944" not in text and "golden necklace" not in text
    shown = re.findall(r"\[(JW\d+)\]", text)
    assert 3 <= len(shown) <= 5 and set(shown) <= {"JW10950", "JW10960", "JW10970"}
    assert router.calls == []
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku is None                                    # nothing was (re-)pinned
    assert conv.last_search["shown_skus"] and "JW10944" not in conv.last_search["shown_skus"]
    assert len(buttons_of(ev)) >= 3                                            # buttons / list rows to pick from


async def test_router_search_catalog_also_excludes_the_ordered_product(replay_http, replay_session, router, outbox, monkeypatch):
    """Even when the ₹0 side-question path steps aside, router search_catalog('necklace') never returns the last-ordered SKU."""
    phone, pnid = _ids(15)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="greeting")
    await seed_paid_order(replay_session, client, conv_id, phone)
    monkeypatch.setattr("app.services.side_questions.detect_topic", lambda *a, **k: None)
    router.when("more necklaces like that please", "search_catalog", {"query": "necklace"}, confidence=0.95)
    router.when("anything like that", "show_product", {"sku": "JW10944"}, confidence=0.95)   # the model's old mistake

    ev = await turn(replay_http, outbox, "more necklaces like that please", phone=phone, pnid=pnid)
    assert "JW10944" not in body(ev) and "[JW10950]" in body(ev)

    ev = await turn(replay_http, outbox, "anything like that", phone=phone, pnid=pnid)
    assert "JW10944" not in body(ev)                                           # show_product is overruled into a search
    assert (await get_conv(replay_session, conv_id)).pending_product_sku is None


async def test_another_one_continues_the_same_search(replay_http, replay_session, router, outbox):
    """'another one?' -> the next items of the SAME search; items already shown are not repeated."""
    phone, pnid = _ids(16)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="greeting")
    await seed_paid_order(replay_session, client, conv_id, phone)

    first = body(await turn(replay_http, outbox, "show me some more products like that", phone=phone, pnid=pnid))
    second = body(await turn(replay_http, outbox, "another one?", phone=phone, pnid=pnid))

    assert "JW10944" not in second
    assert not (set(re.findall(r"\[(JW\d+)\]", first)) & set(re.findall(r"\[(JW\d+)\]", second)))
    assert "catalogue" in second.lower() or "riyasarees" in second               # nothing left -> the catalogue link


# ═════════════════════════════════════════════════════════════════════════════
# P1-3  mid-order product switch
# ═════════════════════════════════════════════════════════════════════════════

async def _collecting(session, client, phone, **extra):
    """Paisley saree order with Red chosen, asking for quantity."""
    return await seed_conv(
        session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
        last_shown_sku="SR63896", **extra,
    )


async def test_another_sku_mid_order_asks_switch_add_both_or_keep(replay_http, replay_session, router, outbox):
    """conv=60 ('JW10944' at 'Size?' silently switched) -> card + Switch / Add both / Keep current, nothing changed yet."""
    phone, pnid = _ids(17)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _collecting(replay_session, client, phone)

    ev = await turn(replay_http, outbox, "JW10944", phone=phone, pnid=pnid)

    text = body(ev)
    assert "golden necklace [JW10944]" in text and "Switch to golden necklace, add both, or keep Paisley Georgette Saree?" in text
    assert "Okay, switching" not in text
    assert buttons_of(ev) == ["Switch", "Add both", "Keep current"]
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.selected_color == "Red" and conv.interrupted_sku == "JW10944"


async def test_keep_current_resumes_the_open_question(replay_http, replay_session, router, outbox):
    """[Keep current] -> the saree order carries on exactly where it was."""
    phone, pnid = _ids(18)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _collecting(replay_session, client, phone)
    await turn(replay_http, outbox, "JW10944", phone=phone, pnid=pnid)

    ev = await tap(replay_http, outbox, phone, pnid, "keep_current", "Keep current")

    assert "Quantity?" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR63896" and conv.selected_color == "Red" and conv.interrupted_sku is None
    assert conv.current_stage == "order_collection"


async def test_switch_moves_the_order_to_the_new_product(replay_http, replay_session, router, outbox):
    """[Switch] -> the necklace becomes the order (fresh slots), the saree is dropped."""
    phone, pnid = _ids(19)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _collecting(replay_session, client, phone)
    await turn(replay_http, outbox, "JW10944", phone=phone, pnid=pnid)

    ev = await tap(replay_http, outbox, phone, pnid, "switch_product", "Switch")

    assert "Colour? Antique / Rose Gold" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "JW10944" and conv.selected_color is None and conv.interrupted_sku is None


async def test_add_both_finishes_the_first_product_then_offers_the_second_in_the_summary(replay_http, replay_session, router, outbox):
    """[Add both] while collecting -> 'finish the saree first'; the summary then offers 'Add golden necklace' (text AND button)."""
    phone, pnid = _ids(20)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await _collecting(replay_session, client, phone)
    await turn(replay_http, outbox, "JW10944", phone=phone, pnid=pnid)

    ev = await tap(replay_http, outbox, phone, pnid, "add_both", "Add both")
    assert "finish Paisley Georgette Saree first" in body(ev) and "Quantity?" in body(ev)
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "SR63896"

    ev = await turn(replay_http, outbox, "1", phone=phone, pnid=pnid)
    text = body(ev)
    assert "2️⃣ Add golden necklace (₹150,000) too" in text
    assert buttons_of(ev) == ["Confirm & Pay", "Add golden necklace", "Cancel"]       # buttons == numbered options


async def test_add_both_at_the_summary_folds_the_cart_and_starts_the_second_product(replay_http, replay_session, router, outbox):
    """At the summary: another SKU -> offer -> [Add both] -> the saree stays in the cart, the necklace is set up next."""
    phone, pnid = _ids(21)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(
        replay_session, client, phone, stage="awaiting_final_confirmation", pending_product_sku="SR63896",
        selected_color="Red", pending_order_quantity=1, payment_method="UPI", summary_shown=True,
    )
    ev = await turn(replay_http, outbox, "JW10944", phone=phone, pnid=pnid)
    assert buttons_of(ev) == ["Switch", "Add both", "Keep current"]

    ev = await tap(replay_http, outbox, phone, pnid, "add_both", "Add both")

    assert "Now let's set up golden necklace" in body(ev) and "Colour? Antique / Rose Gold" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "JW10944"
    assert [(i["sku"], i["qty"], i["color"]) for i in conv.cart_items] == [("SR63896", 1, "Red")]


# ═════════════════════════════════════════════════════════════════════════════
# P1-4  discount follow-up, log hygiene
# ═════════════════════════════════════════════════════════════════════════════

async def test_please_after_a_discount_refusal_is_not_start_order(replay_http, replay_session, router, outbox):
    """conv=60: 'too expensive!' -> fixed price; 'please' (router would say start_order) -> polite repeat + cheaper options."""
    phone, pnid = _ids(22)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="product_inquiry", pending_product_sku="SR63896", last_shown_sku="SR63896")
    router.when("please", "start_order", {"sku": "SR63896"}, confidence=0.95)             # what the live model did

    ev = await turn(replay_http, outbox, "too expensive!", phone=phone, pnid=pnid)
    assert body(ev).count("?") == 1                                                      # one question only
    assert "prices are fixed" in body(ev) and "continue with the order" not in body(ev)

    ev = await turn(replay_http, outbox, "please", phone=phone, pnid=pnid)
    text = body(ev)
    assert "price is the same for everyone" in text and "lower price" in text
    shown = re.findall(r"\[(\w+)\]", text)
    assert shown and "SR63896" not in shown
    assert router.calls == []                                                            # decided without the LLM
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "product_inquiry" and conv.selected_color is None       # no order was started


async def test_discount_mid_order_asks_exactly_one_question(replay_http, replay_session, router, outbox):
    """'give me some discount!' at 'Colour?' -> one statement + the colour question; never also 'continue with the order?'."""
    phone, pnid = _ids(23)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896")

    ev = await turn(replay_http, outbox, "give me some discount!", phone=phone, pnid=pnid)

    text = body(ev)
    assert text.count("?") == 1 and "Colour?" in text and "continue with the order" not in text
    assert text.startswith("Our prices are fixed")


async def test_bare_affirmation_log_shows_what_the_customer_typed(replay_http, replay_session, router, outbox, caplog):
    """The router rewrites 'please' -> 'SR63896'; the 'bare affirmation' log line must still say 'please'."""
    phone, pnid = _ids(24)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="product_inquiry", pending_product_sku="SR63896", last_shown_sku="SR63896")
    router.when("lets do it then", "start_order", {"sku": "SR63896"}, confidence=0.95)
    caplog.set_level(logging.INFO)

    await turn(replay_http, outbox, "lets do it then", phone=phone, pnid=pnid)

    logs = [r.getMessage() for r in caplog.records if "bare affirmation" in r.getMessage() and not r.getMessage().startswith("TURN")]
    assert logs and all("'lets do it then'" in m and "'SR63896'" not in m for m in logs), logs


# ═════════════════════════════════════════════════════════════════════════════
# P2
# ═════════════════════════════════════════════════════════════════════════════

async def test_clarify_never_guesses_a_product_from_chatter(replay_http, replay_session, router, outbox):
    """conv=60: 'i meant to say i cant got it' suggested 'Gota Patti …' ('got' ⊂ 'Gota'). Now: confusion, no product guess."""
    phone, pnid = _ids(25)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="greeting")

    ev = await turn(replay_http, outbox, "i meant to say i cant got it", phone=phone, pnid=pnid)
    assert "Did you mean" not in body(ev) and "Gota" not in body(ev)

    router.when("got patti banarasi", "search_catalog", {"query": "gota"}, confidence=0.3)
    ev = await turn(replay_http, outbox, "got patti banarasi", phone=phone, pnid=pnid)         # a real (partial) name still helps
    assert "Gota" not in body(ev) or "Did you mean" in body(ev)


def test_name_match_score_ignores_short_words_and_chatter():
    """Unit: 'got' / 'get' / 'it' / 'the' / 'i' do not count; a real product name scores 1.0."""
    from types import SimpleNamespace

    from app.services import catalogue_service as cs

    gota = SimpleNamespace(name="Gota Patti Banarasi Dupatta", category="Dupatta", description=None, is_active=True)
    assert cs.name_match_score(gota, "i meant to say i cant got it") == 0.0
    assert cs.name_match_score(gota, "can i get the it") == 0.0
    assert cs.name_match_score(gota, "gota patti dupatta") == 1.0
    hits = cs.search_products_with_scores([gota], "got it")
    assert hits == []                                                                           # no substring match inside "Gota"


async def test_summary_says_upi_only_once_and_its_buttons_match_the_numbered_options(replay_http, replay_session, router, outbox):
    """No 'We currently accept payments via UPI only.' paragraph; the 💳 line says 'UPI only'; text options == buttons."""
    phone, pnid = _ids(26)
    client = await seed_shop(replay_session, phone, pnid)
    await seed_conv(replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896", selected_color="Red",
                    browsed_skus=json.dumps(["JW10944"]))

    ev = await turn(replay_http, outbox, "1", phone=phone, pnid=pnid)

    text = body(ev)
    assert "We currently accept payments via UPI only" not in text
    assert "💳 UPI only (riyasarees@paytm)" in text and text.count("UPI only") == 1
    assert "2️⃣ Add golden necklace (₹150,000) too" in text
    assert buttons_of(ev) == ["Confirm & Pay", "Add golden necklace", "Cancel"]


async def test_add_button_tap_adds_the_second_product(replay_http, replay_session, router, outbox):
    """[Add golden necklace] works like typing '2'."""
    phone, pnid = _ids(27)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="order_collection", pending_product_sku="SR63896",
                              selected_color="Red", browsed_skus=json.dumps(["JW10944"]))
    await turn(replay_http, outbox, "1", phone=phone, pnid=pnid)

    ev = await tap(replay_http, outbox, phone, pnid, "add_crosssell", "Add golden necklace")

    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "JW10944" and [i["sku"] for i in conv.cart_items] == ["SR63896"]
    assert "Colour? Antique / Rose Gold" in body(ev)


async def test_delivery_window_of_one_day_prints_a_single_date(replay_http, replay_session, router, outbox):
    """conv=60: 'Expected delivery: 20 Oct – 20 Oct.' -> '20 Oct'; the summary's days and the status ETA share one source."""
    phone, pnid = _ids(28)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="greeting")
    await seed_paid_order(replay_session, client, conv_id, phone)
    router.when("when will i receive my order?", "order_status", {"focus": "delivery"}, confidence=0.95)

    ev = await turn(replay_http, outbox, "when will i receive my order?", phone=phone, pnid=pnid)

    text = body(ev)
    assert re.search(r"Expected delivery: \d{2} \w{3}\.", text) and not re.search(r"(\d{2} \w{3}) – \1", text), text


def test_eta_line_and_delivery_phrase_helpers():
    """Unit: same start/end -> one date; delivery phrase shares order_delivery_days (2/2 -> '2 business days')."""
    from datetime import date
    from types import SimpleNamespace

    from app.services.delivery_service import get_delivery_time_str, order_delivery_days
    from app.services.router_actions import eta_window_line

    assert eta_window_line("english", date(2026, 10, 20), date(2026, 10, 20)) == "📦 Expected delivery: 20 Oct."
    assert eta_window_line("english", date(2026, 10, 20), date(2026, 10, 24)) == "📦 Expected delivery: 20 Oct – 24 Oct."
    necklace = SimpleNamespace(delivery_days=2)
    assert get_delivery_time_str(necklace, None) == "2 business days"
    assert order_delivery_days(None, [necklace.delivery_days]) == (2, 2)
    client = SimpleNamespace(delivery_days_min=2, delivery_days_max=2)
    assert get_delivery_time_str(None, client) == "2 business days"
    assert get_delivery_time_str(None, SimpleNamespace(delivery_days_min=3, delivery_days_max=7)) == "3–7 business days"


async def _payment_submitted_order(session, client, conv_id, phone, total):
    """An order waiting for seller verification."""
    from app.models.order import Order

    now = datetime.now(timezone.utc)
    order = Order(
        order_number="ORD-2026-0101", client_id=client.id, conversation_id=conv_id, customer_name="Amit",
        customer_phone=phone, delivery_address="702 Somerest, Ahmedabad", product_name="golden necklace",
        product_sku="JW10944", quantity=1, unit_price=total, total_amount=total, payment_method="UPI",
        status="pending_payment", created_at=now,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def test_high_value_order_needs_manual_review_before_approval(replay_http, replay_session, router, outbox):
    """₹150,000: the screenshot ack says 'checked manually'; a plain approve raises; the explicit review approves + audits."""
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.services import payment_verification_service as pvs
    from tests.replay.helpers import send_image

    from app.models.client import Client

    phone, pnid = _ids(29)
    client = await seed_shop(replay_session, phone, pnid)
    client_id = client.id
    conv_id = await seed_conv(replay_session, client, phone, stage="payment", pending_product_sku="JW10944")
    order = await _payment_submitted_order(replay_session, client, conv_id, phone, 150000.0)
    order_id = order.id

    n = len(outbox.events)
    resp = await send_image(replay_http, phone, wamid="wamid.flow.proof.1", phone_number_id=pnid)
    assert resp.status_code == 200
    ack = body(outbox.since(n, phone))
    assert "high-value order" in ack and "manually" in ack

    row = (await replay_session.execute(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "payment_submitted" and row.requires_manual_review is True

    client = await replay_session.get(Client, client_id)
    with pytest.raises(pvs.ManualReviewRequired):
        await pvs.approve(replay_session, client, order_id, None)
    await replay_session.rollback()
    row = (await replay_session.execute(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "payment_submitted" and row.paid_at is None                              # still waiting, nothing sent

    client = await replay_session.get(Client, client_id)
    res = await pvs.approve(replay_session, client, order_id, None, confirm_high_value=True)
    assert res.changed and res.order.status == "paid" and res.order.manual_review_cleared_at is not None
    actions = (await replay_session.execute(select(OrderAuditLog.action).where(OrderAuditLog.order_id == order_id))).scalars().all()
    assert "manual_review_required" in actions and "manual_review_cleared" in actions and "payment_approved" in actions


async def test_regular_order_is_approved_without_the_extra_step(replay_http, replay_session, router, outbox):
    """Below the limit nothing changes: plain approve works and the normal acknowledgement is used."""
    from app.services import payment_verification_service as pvs
    from tests.replay.helpers import send_image

    from app.models.client import Client

    phone, pnid = _ids(30)
    client = await seed_shop(replay_session, phone, pnid)
    client_id = client.id
    conv_id = await seed_conv(replay_session, client, phone, stage="payment", pending_product_sku="SR63896")
    order = await _payment_submitted_order(replay_session, client, conv_id, phone, 5599.0)
    order_id = order.id
    n = len(outbox.events)
    await send_image(replay_http, phone, wamid="wamid.flow.proof.2", phone_number_id=pnid)
    assert "high-value" not in body(outbox.since(n, phone))

    client = await replay_session.get(Client, client_id)
    res = await pvs.approve(replay_session, client, order_id, None)

    assert res.changed and res.order.status == "paid" and not res.order.requires_manual_review


async def test_high_value_threshold_is_configurable_per_shop(replay_http, replay_session, router, outbox):
    """Client.high_value_order_threshold overrides the ₹50,000 default."""
    from types import SimpleNamespace

    from app.services import payment_verification_service as pvs

    assert pvs.high_value_threshold(None) == 50000.0
    assert pvs.high_value_threshold(SimpleNamespace(high_value_order_threshold=2000)) == 2000.0
    assert pvs.is_high_value(SimpleNamespace(total_amount=2001), SimpleNamespace(high_value_order_threshold=2000))
    assert not pvs.is_high_value(SimpleNamespace(total_amount=50000), None)


# ═════════════════════════════════════════════════════════════════════════════
# conv=60 transcript fixture
# ═════════════════════════════════════════════════════════════════════════════

def _load(name: str) -> dict:
    """A recorded transcript segment (customer turns, the router verdict the live model gave, expectations)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def _play(http, session, box, router, phone, pnid, conv_id, steps: list[dict]) -> list[str]:
    """Play the recorded customer turns; check each step's expectations. Returns the reply text of every step."""
    replies: list[str] = []
    for i, step in enumerate(steps):
        if step.get("router"):
            r = step["router"]
            router.when(step["say"], r["action"], r.get("args"), confidence=r.get("confidence", 0.95))
        if step.get("tap"):
            ev = await tap(http, box, phone, pnid, step["tap"]["id"], step["tap"]["title"])
        else:
            ev = await turn(http, box, step["say"], phone=phone, pnid=pnid)
        text = body(ev)
        replies.append(text)
        label = f"step {i} {step.get('say') or step.get('tap')!r}: {text!r}"
        for needle in step.get("contains", []):
            assert needle in text, f"{label} should contain {needle!r}"
        for needle in step.get("not_contains", []):
            assert needle not in text, f"{label} must not contain {needle!r}"
        for pattern in step.get("not_matches", []):
            assert not re.search(pattern, text), f"{label} must not match {pattern!r}"
        if step.get("buttons") is not None:
            assert buttons_of(ev) == step["buttons"], label
        if step.get("state"):
            conv = await get_conv(session, conv_id)
            for column, expected in step["state"].items():
                assert getattr(conv, column) == expected, f"{label}: conv.{column}={getattr(conv, column)!r}, expected {expected!r}"
    return replies


async def test_conv60_after_the_order_browsing_segment(replay_http, replay_session, router, outbox):
    """Customer turns 4397–4416 of conv=60: delivery question, thanks, 'more products', 'another one?', discount, 'please'."""
    fx = _load("conv60_2026_10_17.json")
    phone, pnid = _ids(31)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="greeting")
    await seed_paid_order(replay_session, client, conv_id, phone)

    replies = await _play(replay_http, replay_session, outbox, router, phone, pnid, conv_id, fx["browsing_after_order"])

    assert len(replies) == len(fx["browsing_after_order"])


async def test_conv60_order_confusion_cancel_and_restore_segment(replay_http, replay_session, router, outbox):
    """Customer turns 4417–4447 of conv=60: qty change, 'different', 'sorry i cant get it!', 'continue this', and finishing."""
    fx = _load("conv60_2026_10_17.json")
    phone, pnid = _ids(32)
    client = await seed_shop(replay_session, phone, pnid)
    conv_id = await seed_conv(replay_session, client, phone, stage="product_inquiry", pending_product_sku="SR63896", last_shown_sku="SR63896")

    replies = await _play(replay_http, replay_session, outbox, router, phone, pnid, conv_id, fx["order_confusion_cancel_restore"])

    assert len(replies) == len(fx["order_confusion_cancel_restore"])
