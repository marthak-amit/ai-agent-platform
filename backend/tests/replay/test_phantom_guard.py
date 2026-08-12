"""
FIX 1 — Product hallucination guard tests.
FIX 2 — WhatsApp confirm/cancel button tests.

All tests hit a real Postgres instance (replay_ci_test).
Skipped automatically when local Postgres is not reachable.

FIX 1 assertions:
  - test_no_phantom_products: seed 1 product (SR31045). AI returns a reply that
    includes two phantom SKUs (SR31046, SR31047). Guard must strip them and the
    delivered reply must contain ONLY SR31045.
  - test_list_all_real: seed 3 products. AI returns a clean reply listing all 3.
    Guard must pass it through unchanged.
  - Assertion helper: extract_skus_and_prices(text) — all extracted tokens must
    exist in the seeded catalogue.
  - test_resolved_multi_choice_pick_not_phantom_guarded: a SKU resolved from a
    pending multi-choice list pick (button tap) must NOT be rejected as
    "phantom" by guard_product_reply — regression test for the bug where
    canonical_browse_products stayed stale (from before the pick resolved) so
    the just-confirmed SKU's own FIX1 pinned-fact reply was overridden with a
    false "we don't carry it" message.

FIX 2 assertions:
  - test_button_confirm: interactive button_reply.id="confirm_pay" at
    awaiting_final_confirmation → order advanced to payment/completed.
  - test_button_cancel: button_reply.id="cancel_order" → order cancelled,
    stage reset to greeting.
  - test_typed_fallback_still_works: typing "1" still confirms; "2" still cancels.
"""

from __future__ import annotations

import re
import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, WA_PHONE_NUMBER_ID
from tests.replay.helpers import capture_all, send_button, send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)

# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------

_SKU_RE = re.compile(r"\b([A-Za-z]{2,4}\d{4,6})\b")
_PRICE_RE = re.compile(r"₹\s*(\d[\d,]*)")


def extract_skus(text: str) -> set[str]:
    """Extract all SKU-like tokens from text (uppercased)."""
    return {m.group(1).upper() for m in _SKU_RE.finditer(text)}


def extract_prices(text: str) -> set[str]:
    """Extract ₹-prefixed numeric amounts (comma-stripped) from text."""
    return {m.group(1).replace(",", "") for m in _PRICE_RE.finditer(text)}


def assert_no_phantom_data(reply: str, allowed_skus: set[str], allowed_prices: set[str]) -> None:
    """Assert that reply contains only SKUs and prices present in the seeded catalogue."""
    found_skus = extract_skus(reply)
    found_prices = extract_prices(reply)
    phantom_skus = found_skus - allowed_skus
    phantom_prices = found_prices - allowed_prices
    assert not phantom_skus, (
        f"Phantom SKUs in reply: {phantom_skus!r}\nReply: {reply!r}"
    )
    assert not phantom_prices, (
        f"Phantom prices in reply: {phantom_prices!r}\nReply: {reply!r}"
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _phone(tag: str) -> str:
    return f"91800{tag}"


def _pnid(tag: str) -> str:
    return f"phantom{tag}"


async def _seed_georgette(session: AsyncSession, *, phone: str, pnid: str):
    """Seed client + single 'Georgette Party Wear' product (SR31045)."""
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Phantom Guard Store",
        email=f"phantom_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
        upi_id=None,
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="Georgette Party Wear",
        sku="SR31045",
        price=1200.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    session.add(product)
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


async def _seed_three_products(session: AsyncSession, *, phone: str, pnid: str):
    """Seed client + 3 saree products for test_list_all_real."""
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Three Product Store",
        email=f"three_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
        upi_id=None,
    )
    session.add(client)
    await session.flush()

    products = []
    for sku, name, price in [
        ("SR10001", "Cotton Saree", 800.0),
        ("SR10002", "Silk Saree", 1500.0),
        ("SR10003", "Banarasi Saree", 2200.0),
    ]:
        p = Product(
            client_id=client.id,
            name=name,
            sku=sku,
            price=price,
            stock=5,
            is_active=True,
            has_variants=False,
        )
        session.add(p)
        products.append(p)
    await session.commit()
    await session.refresh(client)
    for p in products:
        await session.refresh(p)
    return client, products


async def _seed_confirmation_client(session: AsyncSession, *, phone: str, pnid: str):
    """Seed client + product + pre-loaded AFC conversation for button tests."""
    from app.models.client import Client
    from app.models.product import Product
    from app.models.conversation import Conversation

    client = Client(
        business_name="Button Test Store",
        email=f"btn_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
        upi_id="test@upi",
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="Test Kurta",
        sku="KU99001",
        price=500.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    session.add(product)
    await session.flush()

    # Pre-load a conversation with all slots filled, at awaiting_final_confirmation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=client.id,
        current_stage="awaiting_final_confirmation",
        pending_product_sku="KU99001",
        pending_order_quantity=2,
        customer_name="Amit",
        delivery_address="702 Test St, Mumbai",
        payment_method="COD",
        summary_shown=True,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    await session.refresh(conv)
    return client, product, conv


# ---------------------------------------------------------------------------
# FIX 1 — Phantom product guard
# ---------------------------------------------------------------------------

async def test_no_phantom_products(replay_http, replay_session, monkeypatch):
    """
    Seed exactly 1 product (SR31045 at ₹1200).
    Make the AI return a reply that includes two invented SKUs (SR31046, SR31047).
    Guard must intercept and the delivered reply must contain ONLY SR31045.
    """
    phone = _phone("00001")
    pnid = _pnid("00001")
    client, product = await _seed_georgette(
        replay_session, phone=phone, pnid=pnid
    )

    # Configure mock to return a hallucinated product list
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=(
            "Here are some Georgette options:\n"
            "• Georgette Party Wear [SR31045] — ₹1200\n"
            "• Designer Georgette [SR31046] — ₹1500\n"
            "• Premium Georgette [SR31047] — ₹1800\n"
            "Would you like to order?"
        )),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(replay_http, phone, "Georgette Party Wear", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text

    assert captured, "No reply was sent"
    reply = captured[-1]

    allowed_skus = {"SR31045"}
    allowed_prices = {"1200"}

    assert "SR31045" in reply, f"Real SKU SR31045 missing from reply: {reply!r}"
    assert_no_phantom_data(reply, allowed_skus, allowed_prices)


async def test_list_all_real(replay_http, replay_session, monkeypatch):
    """
    Seed 3 products (SR10001, SR10002, SR10003).
    AI returns a clean reply listing all 3 real SKUs and real prices.
    Guard must pass it through unchanged — all 3 real SKUs present, no invented ones.
    """
    phone = _phone("00002")
    pnid = _pnid("00002")
    client, products = await _seed_three_products(
        replay_session, phone=phone, pnid=pnid
    )

    # Clean AI understanding — Section 1: the LLM returns structured JSON
    # (never prose); render_reply.render_product_list_reply() builds the
    # actual customer-facing text from the DB-fetched product rows.
    import json as _json
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=_json.dumps({
            "intent": "LIST_PRODUCTS",
            "sku": None,
            "skus": ["SR10001", "SR10002", "SR10003"],
            "slots": {"color": None, "size": None, "quantity": None},
            "question_topic": None,
        })),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(replay_http, phone, "saree dikhao", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text

    assert captured, "No reply was sent"
    reply = captured[-1]

    allowed_skus = {"SR10001", "SR10002", "SR10003"}
    allowed_prices = {"800", "1500", "2200"}

    for sku in allowed_skus:
        assert sku in reply, f"Real SKU {sku} missing from reply: {reply!r}"
    assert_no_phantom_data(reply, allowed_skus, allowed_prices)


async def test_resolved_multi_choice_pick_not_phantom_guarded(replay_http, replay_session, monkeypatch):
    """
    BUG 1 regression test.

    Reproduces the exact conv=60-style scenario: the customer already has an
    UNRELATED product (SR00100, a "Formal Shirt") pinned as pending_product_sku
    from earlier in the conversation. They then ask an ambiguous multi-word
    "saree" query, which opens a pending_choice_skus list for two Saree
    products WITHOUT touching pending_product_sku (still SR00100). They pick
    one of the Saree options via a list_reply button tap.

    _get_catalogue_context() runs at the START of the pick turn, before the
    pick resolves — with pending_product_sku still SR00100, its step-1 short
    circuit returns canonical_browse_products=[SR00100 shirt] immediately,
    never reaching the picked Saree product. Once the pick resolves this same
    turn, FIX1 fires a deterministic pinned-fact reply containing the picked
    Saree's own real SKU/price — but guard_product_reply() then checks that
    reply against canonical_browse_products, which (pre-fix) is still just
    the unrelated shirt. The picked Saree's SKU/price aren't in that stale
    allowed set, so the guard flags them as phantom and replaces the reply
    with a false "we don't carry it" message, even though the SKU was just
    correctly resolved from the shown list. After the fix, the pick-resolution
    branch rebuilds canonical_browse_products from the just-picked product, so
    the reply passes through untouched.
    """
    phone = _phone("00003")
    pnid = _pnid("00003")
    from app.models.client import Client
    from app.models.product import Product
    from app.models.conversation import Conversation

    client = Client(
        business_name="Resolved Pick Store",
        email=f"resolvedpick_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
        upi_id=None,
    )
    replay_session.add(client)
    await replay_session.flush()

    prod_unrelated = Product(
        client_id=client.id, name="Formal Shirt", sku="SR00100",
        price=699.0, stock=5, is_active=True, has_variants=False,
    )
    prod_a = Product(
        client_id=client.id, name="Cotton Saree", sku="SR33210",
        price=999.0, stock=5, is_active=True, has_variants=False,
    )
    prod_b = Product(
        client_id=client.id, name="Silk Saree", sku="SR33299",
        price=1999.0, stock=5, is_active=True, has_variants=False,
    )
    replay_session.add_all([prod_unrelated, prod_a, prod_b])
    await replay_session.commit()
    await replay_session.refresh(prod_unrelated)
    await replay_session.refresh(prod_a)
    await replay_session.refresh(prod_b)

    # Conversation already has an UNRELATED product pinned (mirrors a
    # customer mid-browse who had already asked about the shirt earlier).
    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=client.id,
        current_stage="product_inquiry", pending_product_sku="SR00100",
    )
    replay_session.add(conv)
    await replay_session.commit()

    captured = capture_all(monkeypatch)

    # Multi-word so it doesn't hit the bare-single-word "_active_order_context"
    # block that would otherwise skip the name-match pinner entirely while a
    # product is already pinned.
    resp = await send_message(
        replay_http, phone, "saree please", phone_number_id=pnid,
        wamid=f"wamid.rp.list.{int(time.time())}",
    )
    assert resp.status_code == 200, resp.text

    conv_after_list = await _get_conv_by_phone(replay_session, phone)
    assert conv_after_list.pending_choice_skus, (
        f"Expected pending_choice_skus set, got {conv_after_list.pending_choice_skus!r}"
    )
    assert conv_after_list.pending_product_sku == "SR00100", (
        "The ambiguous multi-choice branch must not touch the already-pinned "
        f"unrelated SKU; got {conv_after_list.pending_product_sku!r}"
    )
    import json as _json
    choice_list = _json.loads(conv_after_list.pending_choice_skus)
    assert len(choice_list) >= 2, f"Expected 2+ choice SKUs, got {choice_list!r}"
    target_sku = choice_list[0]

    resp2 = await send_button(
        replay_http, phone, target_sku, f"{target_sku} option",
        phone_number_id=pnid, wamid=f"wamid.rp.pick.{int(time.time())}",
    )
    assert resp2.status_code == 200, resp2.text

    conv_after_pick = await _get_conv_by_phone(replay_session, phone)
    assert conv_after_pick.pending_product_sku == target_sku, (
        f"Button tap must pin pending_product_sku={target_sku!r}, "
        f"got {conv_after_pick.pending_product_sku!r}"
    )

    reply = captured[-1] if captured else ""
    assert target_sku in reply, (
        f"Picked SKU {target_sku!r} missing from post-pick reply: {reply!r}"
    )
    assert "don't carry" not in reply.lower() and "we currently have" not in reply.lower(), (
        f"BUG 1: resolved multi-choice pick {target_sku!r} was overridden by "
        f"guard_product_reply's false phantom/not-found path (guarded against the "
        f"stale unrelated pinned product SR00100 instead of the just-picked SKU): "
        f"{reply!r}"
    )


async def _get_conv_by_phone(session: AsyncSession, phone: str):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one_or_none()


# ---------------------------------------------------------------------------
# FIX 2 — WhatsApp confirm/cancel buttons
# ---------------------------------------------------------------------------

async def test_button_confirm(replay_http, replay_session, monkeypatch):
    """
    Interactive button_reply.id='confirm_pay' at awaiting_final_confirmation
    must advance the order to the next stage (completed/payment) exactly like
    typing '1'.
    """
    phone = _phone("00010")
    pnid = _pnid("00010")
    client, product, conv = await _seed_confirmation_client(
        replay_session, phone=phone, pnid=pnid
    )

    captured: list[str] = []

    async def _capture(to_phone_number, message_text):
        captured.append(message_text)

    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_text_message",
        _capture,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_button_message",
        mock.AsyncMock(return_value=True),
    )

    resp = await send_button(
        replay_http, phone, "confirm_pay", "Confirm & pay", phone_number_id=pnid
    )
    assert resp.status_code == 200, resp.text

    # After confirm, conversation must NOT still be at awaiting_final_confirmation
    from app.models.conversation import Conversation
    result = await replay_session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    updated_conv = result.scalar_one_or_none()
    assert updated_conv is not None
    assert updated_conv.current_stage in ("completed", "payment", "greeting"), (
        f"Expected order advanced, got stage={updated_conv.current_stage!r}"
    )

    # An order row must have been created
    from app.models.order import Order
    orders_result = await replay_session.execute(
        select(Order).where(Order.conversation_id == conv.id)
        .execution_options(populate_existing=True)
    )
    orders = list(orders_result.scalars().all())
    assert orders, "No order row created after confirm_pay button"


async def test_button_cancel(replay_http, replay_session, monkeypatch):
    """
    Interactive button_reply.id='cancel_order' at awaiting_final_confirmation
    must cancel the order, clear all slots, and reset stage to greeting.
    """
    phone = _phone("00011")
    pnid = _pnid("00011")
    client, product, conv = await _seed_confirmation_client(
        replay_session, phone=phone, pnid=pnid
    )

    captured: list[str] = []

    async def _capture(to_phone_number, message_text):
        captured.append(message_text)

    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_text_message",
        _capture,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_button_message",
        mock.AsyncMock(return_value=True),
    )

    resp = await send_button(
        replay_http, phone, "cancel_order", "Cancel", phone_number_id=pnid
    )
    assert resp.status_code == 200, resp.text

    from app.models.conversation import Conversation
    result = await replay_session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    updated_conv = result.scalar_one_or_none()
    assert updated_conv is not None
    assert updated_conv.current_stage == "greeting", (
        f"Expected stage=greeting after cancel, got {updated_conv.current_stage!r}"
    )
    assert updated_conv.pending_product_sku is None, "pending_product_sku must be cleared after cancel"
    assert updated_conv.customer_name is None, "customer_name must be cleared after cancel"
    assert updated_conv.delivery_address is None, "delivery_address must be cleared after cancel"
    assert (updated_conv.pending_order_quantity or 0) == 0, "quantity must be cleared after cancel"

    assert captured, "No cancel acknowledgement was sent"
    cancel_reply = captured[-1]
    assert any(w in cancel_reply.lower() for w in ("cancel", "ரத்", "रद्द")), (
        f"Expected cancel confirmation in reply: {cancel_reply!r}"
    )


async def test_typed_fallback_still_works(replay_http, replay_session, monkeypatch):
    """
    Typing '1' at awaiting_final_confirmation must still confirm the order
    (typed fallback must remain functional alongside button taps).
    Typing '2' must still cancel.
    """
    # --- Confirm via typed "1" ---
    phone_c = _phone("00020")
    pnid_c = _pnid("00020")
    client_c, product_c, conv_c = await _seed_confirmation_client(
        replay_session, phone=phone_c, pnid=pnid_c
    )

    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_text_message",
        mock.AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_button_message",
        mock.AsyncMock(return_value=True),
    )

    resp = await send_message(replay_http, phone_c, "1", phone_number_id=pnid_c)
    assert resp.status_code == 200

    from app.models.conversation import Conversation as _Conv
    from app.models.order import Order as _Order

    r = await replay_session.execute(
        select(_Conv).where(_Conv.phone_number == phone_c)
        .execution_options(populate_existing=True)
    )
    conv_after = r.scalar_one_or_none()
    assert conv_after.current_stage in ("completed", "payment", "greeting"), (
        f"Typed '1' did not confirm: stage={conv_after.current_stage!r}"
    )

    orders_r = await replay_session.execute(
        select(_Order).where(_Order.conversation_id == conv_c.id)
        .execution_options(populate_existing=True)
    )
    assert list(orders_r.scalars().all()), "No order created for typed '1' confirm"

    # --- Cancel via typed "2" ---
    phone_x = _phone("00021")
    pnid_x = _pnid("00021")
    _, _, conv_x = await _seed_confirmation_client(
        replay_session, phone=phone_x, pnid=pnid_x
    )

    resp2 = await send_message(replay_http, phone_x, "2", phone_number_id=pnid_x)
    assert resp2.status_code == 200

    r2 = await replay_session.execute(
        select(_Conv).where(_Conv.phone_number == phone_x)
        .execution_options(populate_existing=True)
    )
    conv_x_after = r2.scalar_one_or_none()
    assert conv_x_after.current_stage == "greeting", (
        f"Typed '2' did not cancel: stage={conv_x_after.current_stage!r}"
    )
