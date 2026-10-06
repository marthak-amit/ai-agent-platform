"""
Golden replay for the conv=60 catalog-matching bugs (A–D).

Per-turn routing order under test (all deterministic, no LLM):
  greeting -> order status -> exact SKU -> exact product name -> fuzzy -> llm

Each test drives real webhook POSTs and asserts the DB end-state plus the
reply type, so a regression in any step shows up as a changed state/reply
rather than a changed log line.
"""

from __future__ import annotations

import logging
import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_message

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

REASK = "please reply with the number of your choice"


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay (asyncio.sleep(random.uniform(min, max)))."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


def _phone(suffix: str) -> str:
    return f"91930000{suffix}"


def _pnid(suffix: str) -> str:
    return f"444{suffix}"


async def _seed_store(session: AsyncSession, *, suffix: str, upi: bool = True):
    """Client + catalogue: 3 kurtis (one named exactly 'Kurti New One'), a saree pair, PR10983."""
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Routing Store",
        email=f"routing_{suffix}@test.com",
        phone=_phone(suffix),
        whatsapp_phone_number_id=_pnid(suffix),
        accepts_cod=True,
        hashed_password="x",
        upi_id="routing@upi" if upi else None,
    )
    session.add(client)
    await session.flush()
    rows = [
        ("Kurti New One", "KU76326", 899.0),
        ("Kurti Cotton Blue", "KU23444", 699.0),
        ("Kurti Party Wear", "KU55512", 1299.0),
        ("Cotton Saree", "SA10001", 800.0),
        ("Silk Saree", "SA10002", 1500.0),
        ("Designer Lehenga", "PR10983", 4999.0),
    ]
    for name, sku, price in rows:
        session.add(Product(
            client_id=client.id, name=name, sku=sku, price=price, stock=10,
            is_active=True, has_variants=False,
        ))
    await session.commit()
    return client


async def _seed_order(session: AsyncSession, client, *, suffix: str, status: str, number: str = "ORD-2026-0001"):
    """One single-item order for this sender in the given status."""
    from app.models.order import Order

    order = Order(
        order_number=number, client_id=client.id, customer_name="Test User",
        customer_phone=_phone(suffix), delivery_address="12 MG Road, Pune",
        product_name="Kurti Cotton Blue", product_sku="KU23444", quantity=2,
        unit_price=699.0, total_amount=1398.0, payment_method="UPI",
        status=status,
    )
    session.add(order)
    await session.commit()
    return order


async def _conv(session: AsyncSession, suffix: str):
    from app.models.conversation import Conversation

    r = await session.execute(
        select(Conversation)
        .where(Conversation.phone_number == _phone(suffix))
        .execution_options(populate_existing=True)
    )
    return r.scalar_one_or_none()


async def _say(http, suffix: str, text: str) -> str:
    """Send one inbound text and return everything the bot sent back (text + button bodies)."""
    from app.services import whatsapp_service as ws

    ws._raw_send_text_message.reset_mock()
    ws._raw_send_button_message.reset_mock()
    resp = await send_message(
        http, _phone(suffix), text, wamid=f"wamid.r60.{suffix}.{time.time_ns()}",
        phone_number_id=_pnid(suffix),
    )
    assert resp.status_code == 200, resp.text
    parts = []
    for c in ws._raw_send_text_message.call_args_list:
        a, k = c
        parts.append(k.get("message_text") or (a[1] if len(a) > 1 else ""))
    for c in ws._raw_send_button_message.call_args_list:
        a, k = c
        parts.append(k.get("body_text") or (a[1] if len(a) > 1 else ""))
    return "\n".join(parts)


def _steps(caplog) -> list[str]:
    """ROUTE_STEP log lines emitted so far."""
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("ROUTE_STEP")]


# ---------------------------------------------------------------------------
# A — "Hi" must greet, never run the catalog name-match
# ---------------------------------------------------------------------------

async def test_hi_is_greeting_and_sets_no_pending_choices(replay_http, replay_session, caplog):
    """'Hi' hits several names by substring ('Chiffon…', 'Chikankari…') yet must greet and open no menu."""
    from app.models.product import Product

    client = await _seed_store(replay_session, suffix="0001")
    for i, name in enumerate(("Chiffon Dupatta", "Chikankari Kurta", "Khimar Chic Set")):
        replay_session.add(Product(
            client_id=client.id, name=name, sku=f"HI{i}0000{i}", price=500.0, stock=5,
            is_active=True, has_variants=False,
        ))
    await replay_session.commit()

    caplog.set_level(logging.INFO)
    reply = await _say(replay_http, "0001", "Hi")

    assert "Welcome to Routing Store" in reply
    assert REASK not in reply.lower()
    conv = await _conv(replay_session, "0001")
    assert conv.pending_choice_skus is None
    assert conv.pending_product_sku is None
    assert any("step=greeting outcome=template" in line for line in _steps(caplog))


async def test_hi_closes_an_open_menu(replay_http, replay_session):
    """A greeting while a 'which one?' menu is open greets and closes the menu (no short re-ask)."""
    await _seed_store(replay_session, suffix="0002")
    await _say(replay_http, "0002", "saree")
    assert (await _conv(replay_session, "0002")).pending_choice_skus

    reply = await _say(replay_http, "0002", "Hi")

    assert "Welcome to Routing Store" in reply
    conv = await _conv(replay_session, "0002")
    assert conv.pending_choice_skus is None
    assert not conv.pending_choice_greeting_count


# ---------------------------------------------------------------------------
# B — order-status question is never swallowed by a stale menu
# ---------------------------------------------------------------------------

async def test_hi_then_order_status_returns_order_status(replay_http, replay_session, caplog):
    """'Hi' then 'what is the status of my order?' -> deterministic status reply from the DB."""
    client = await _seed_store(replay_session, suffix="0003")
    await _seed_order(replay_session, client, suffix="0003", status="confirmed")

    caplog.set_level(logging.INFO)
    await _say(replay_http, "0003", "Hi")
    reply = await _say(replay_http, "0003", "what is the status of my order?")

    assert "ORD-2026-0001" in reply
    assert "Kurti Cotton Blue" in reply
    assert "Confirmed" in reply
    assert REASK not in reply.lower()
    assert (await _conv(replay_session, "0003")).pending_choice_skus is None
    assert any("step=status outcome=found" in line for line in _steps(caplog))


async def test_order_status_with_open_menu_clears_menu(replay_http, replay_session):
    """conv=60 exactly: menu open, then a status question -> status reply, menu cleared."""
    client = await _seed_store(replay_session, suffix="0004")
    await _seed_order(replay_session, client, suffix="0004", status="paid")
    await _say(replay_http, "0004", "saree")
    assert (await _conv(replay_session, "0004")).pending_choice_skus

    reply = await _say(replay_http, "0004", "what is the status of my order?")

    assert "ORD-2026-0001" in reply and "Paid" in reply
    assert REASK not in reply.lower()
    assert (await _conv(replay_session, "0004")).pending_choice_skus is None


async def test_order_status_no_orders_says_none_found(replay_http, replay_session):
    """Status question from a customer with no orders -> the no_orders template."""
    await _seed_store(replay_session, suffix="0005")

    reply = await _say(replay_http, "0005", "where is my order?")

    assert "don't have any orders yet" in reply
    assert "ORD-" not in reply


async def test_order_status_pending_payment_reminds_payment(replay_http, replay_session):
    """pending_payment order -> status + UPI payment reminder (amount, UPI id, send screenshot)."""
    client = await _seed_store(replay_session, suffix="0006")
    await _seed_order(replay_session, client, suffix="0006", status="pending_payment")

    reply = await _say(replay_http, "0006", "order status")

    assert "ORD-2026-0001" in reply
    assert "Pending payment" in reply
    assert "routing@upi" in reply and "1,398" in reply
    assert "screenshot" in reply.lower()


async def test_order_status_by_order_id_and_hindi_phrase(replay_http, replay_session):
    """An ORD-id and a Hinglish phrase both route to status; the id picks that exact order."""
    client = await _seed_store(replay_session, suffix="0007")
    await _seed_order(replay_session, client, suffix="0007", status="delivered", number="ORD-2026-0001")
    await _seed_order(replay_session, client, suffix="0007", status="paid", number="ORD-2026-0002")

    by_id = await _say(replay_http, "0007", "ORD-2026-0001")
    assert "ORD-2026-0001" in by_id and "Delivered" in by_id

    latest = await _say(replay_http, "0007", "mera order kaha hai")
    assert "ORD-2026-0002" in latest


async def test_order_status_never_shows_another_customers_order(replay_http, replay_session):
    """An ORD-id belonging to a different phone reads as 'not found', never leaks the order."""
    client = await _seed_store(replay_session, suffix="0008")
    await _seed_order(replay_session, client, suffix="9999", status="paid", number="ORD-2026-0777")

    reply = await _say(replay_http, "0008", "status of ORD-2026-0777")

    assert "couldn't find order #ORD-2026-0777" in reply
    assert "Kurti Cotton Blue" not in reply


# ---------------------------------------------------------------------------
# C — exact product name: direct product, no menu, no loop
# ---------------------------------------------------------------------------

async def test_exact_product_name_goes_direct_no_menu(replay_http, replay_session, caplog):
    """'Kurti New One' shares 'kurti' with two other products yet pins directly (case/punctuation-insensitive)."""
    await _seed_store(replay_session, suffix="0009")

    caplog.set_level(logging.INFO)
    reply = await _say(replay_http, "0009", "  kurti   NEW one! ")

    assert "KU76326" in reply and "899" in reply
    assert REASK not in reply.lower()
    conv = await _conv(replay_session, "0009")
    assert conv.pending_product_sku == "KU76326"
    assert conv.pending_choice_skus is None
    assert any("step=exact_name outcome=pinned" in line for line in _steps(caplog))


async def test_menu_open_typing_exact_option_name_resolves_once(replay_http, replay_session, caplog):
    """Menu open + exact option name -> resolved once, the name-match must not re-open a menu."""
    await _seed_store(replay_session, suffix="0010")
    await _say(replay_http, "0010", "kurti")
    opened = (await _conv(replay_session, "0010")).pending_choice_skus
    assert opened and "KU76326" in opened

    caplog.clear()
    caplog.set_level(logging.INFO)
    reply = await _say(replay_http, "0010", "Kurti New One")

    assert "KU76326" in reply
    assert REASK not in reply.lower()
    conv = await _conv(replay_session, "0010")
    assert conv.pending_product_sku == "KU76326"
    assert conv.pending_choice_skus is None
    messages = [r.getMessage() for r in caplog.records]
    assert sum("Multi-choice resolved" in m for m in messages) == 1
    assert not any("Name-match multi" in m for m in messages), "menu was re-opened after the pick"


# ---------------------------------------------------------------------------
# Stale menu / fuzzy threshold
# ---------------------------------------------------------------------------

async def test_menu_open_unrelated_question_clears_menu_and_is_handled(replay_http, replay_session):
    """Menu open + unrelated question -> menu cleared, the question answered (not re-asked)."""
    await _seed_store(replay_session, suffix="0011")
    await _say(replay_http, "0011", "saree")
    assert (await _conv(replay_session, "0011")).pending_choice_skus

    reply = await _say(replay_http, "0011", "how many days will delivery take?")

    assert REASK not in reply.lower()
    assert "deliver" in reply.lower()
    assert (await _conv(replay_session, "0011")).pending_choice_skus is None


async def test_menu_open_bare_affirmative_is_still_reasked(replay_http, replay_session):
    """The one thing that keeps a menu open: a bare 'yes' is rejected and the list re-shown."""
    await _seed_store(replay_session, suffix="0012")
    await _say(replay_http, "0012", "saree")

    reply = await _say(replay_http, "0012", "yes")

    assert REASK in reply.lower()
    assert (await _conv(replay_session, "0012")).pending_choice_skus


async def test_weak_fuzzy_match_does_not_open_menu(replay_http, replay_session, caplog):
    """Two products each hit by 1 of 4 query keywords (confidence 0.25 < 0.6) -> no menu, no pending_choice_skus."""
    await _seed_store(replay_session, suffix="0013")

    caplog.set_level(logging.INFO)
    await _say(replay_http, "0013", "saree wedding gold embroidery")

    assert (await _conv(replay_session, "0013")).pending_choice_skus is None
    assert any("step=fuzzy outcome=below_threshold" in line for line in _steps(caplog))


async def test_strong_fuzzy_match_still_opens_menu(replay_http, replay_session, caplog):
    """'saree' (confidence 1.0) keeps the numbered menu — the threshold only blocks weak matches."""
    await _seed_store(replay_session, suffix="0014")

    caplog.set_level(logging.INFO)
    reply = await _say(replay_http, "0014", "saree")

    assert REASK in reply.lower()
    assert (await _conv(replay_session, "0014")).pending_choice_skus
    assert any("step=fuzzy outcome=menu" in line for line in _steps(caplog))


# ---------------------------------------------------------------------------
# D — exact SKU: deterministic product reply, never the LLM
# ---------------------------------------------------------------------------

async def test_exact_sku_is_deterministic_product_reply_with_no_llm_call(replay_http, replay_session, caplog):
    """'PR10983' -> product card from the DB; every LLM entry point stays untouched."""
    from app.services import conversation_flow, gemini_service, llm_client, llm_intent

    await _seed_store(replay_session, suffix="0015")

    boom = mock.AsyncMock(side_effect=AssertionError("LLM must not be called for an exact SKU"))
    with mock.patch.object(llm_client, "chat", boom), \
         mock.patch.object(llm_client, "chat_json", boom), \
         mock.patch.object(llm_intent, "classify_turn", boom):
        gemini_service.generate_reply.reset_mock()
        conversation_flow.classify_buy_intent.reset_mock()
        conversation_flow.is_off_topic_message.reset_mock()
        caplog.set_level(logging.INFO)
        reply = await _say(replay_http, "0015", "pr10983")

    assert "Designer Lehenga" in reply and "PR10983" in reply and "4,999" in reply
    assert "__AI_REPLY__" not in reply
    boom.assert_not_called()
    gemini_service.generate_reply.assert_not_called()
    conversation_flow.classify_buy_intent.assert_not_called()
    conversation_flow.is_off_topic_message.assert_not_called()
    conv = await _conv(replay_session, "0015")
    assert conv.pending_product_sku == "PR10983"
    assert conv.pending_choice_skus is None
    steps = _steps(caplog)
    assert any("step=sku outcome=exact" in line for line in steps)
    assert not any("step=llm" in line for line in steps)
