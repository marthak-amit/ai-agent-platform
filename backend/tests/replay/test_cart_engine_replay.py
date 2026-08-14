"""
Phase 1 cart-based order engine — replay tests.

Covers the cart-building flow end-to-end through the real webhook/DB:
same-variant bulk, different-variant loop (with a mid-loop correction),
different-variant batch (LLM breakdown parsing, mocked — Groq is disabled
in the replay harness), a batch sum mismatch, the multi-color single-message
bug fix, and the unprompted-quantity bug fix. Scenario 1 is the N=1
regression guard for the simple/non-cart path, which must stay unchanged.

See /Users/bytes-amit/.claude/plans/radiant-drifting-blanket.md section 10.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest
from sqlalchemy import select

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import capture_all, send_message

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


def _phone(suffix: str) -> str:
    return f"91902{suffix}"


def _pnid(suffix: str) -> str:
    return f"333{suffix}"


async def _msg(http, phone, text, *, pnid, wamid=None):
    return await send_message(http, phone, text, wamid=wamid, phone_number_id=pnid)


async def _seed_bulk_variant_product(session, *, phone, pnid, color_only: bool = False):
    """
    Seed a UPI-only client + one high-stock variant product for cart tests.

    color_only=False (default): colors Red/Blue/Green x sizes L/M/S, stock
    200 per combo — enough headroom for bulk (N up to ~100) scenarios.
    color_only=True: colors Red/Pink only, no size dimension — used for the
    multi-color bug-fix scenarios, which mirror the original bug report
    (single SKU, color-only, "10 red and 10 pink").
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Cart Test Store",
        email=f"cart_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=False,
        hashed_password="x",
        upi_id="cart@upi",
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="Designer Lehenga",
        sku=f"DL_{phone[-4:]}",
        price=650.0,
        stock=1800,
        is_active=True,
        has_variants=True,
    )
    session.add(product)
    await session.flush()

    if color_only:
        for color in ("Red", "Pink"):
            session.add(ProductVariant(
                product_id=product.id, client_id=client.id,
                color=color, size=None, stock=200, is_active=True, price=650.0,
            ))
    else:
        for color in ("Red", "Blue", "Green"):
            for size in ("L", "M", "S"):
                session.add(ProductVariant(
                    product_id=product.id, client_id=client.id,
                    color=color, size=size, stock=200, is_active=True, price=650.0,
                ))
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


async def _prime(session, *, phone, product, stage, **slots):
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage=stage,
        pending_product_sku=product.sku,
        summary_shown=slots.pop("summary_shown", False),
        **slots,
    )
    session.add(conv)
    await session.commit()
    return conv.id


async def _orders(session, conv_id: int):
    from app.models.order import Order
    r = await session.execute(
        select(Order).where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


async def _line_items(session, order_id: int):
    from app.models.order_line_item import OrderLineItem
    r = await session.execute(
        select(OrderLineItem).where(OrderLineItem.order_id == order_id)
        .order_by(OrderLineItem.line_number)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


async def _conv_row(session, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


# ---------------------------------------------------------------------------
# 1. Single item, N=1 — regression guard for the simple/non-cart path.
# ---------------------------------------------------------------------------

async def test_cart_single_item_regression(replay_http, replay_session, monkeypatch):
    """N=1 on a non-variant product never touches cart_items — identical to
    the pre-cart-engine flow, one Order row, one OrderLineItem row."""
    captured = capture_all(monkeypatch)
    phone, pnid = _phone("001"), _pnid("001")
    client, product = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        has_variants=False, stock=10, payment_method="UPI",
    )
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        pending_order_quantity=None, customer_name=None, delivery_address=None,
        payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "1", pnid=pnid, wamid=f"wamid.s1.qty.{ts}")
    await _msg(replay_http, phone, "Amit Shah", pnid=pnid, wamid=f"wamid.s1.name.{ts}")
    await _msg(replay_http, phone, "12 MG Road, Surat 395002", pnid=pnid, wamid=f"wamid.s1.addr.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s1.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s1.paid.{ts}")

    conv = await _conv_row(replay_session, conv_id)
    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.quantity == 1
    assert order.total_amount == order.unit_price
    items = await _line_items(replay_session, order.id)
    assert len(items) == 1
    assert items[0].quantity == 1
    assert not getattr(conv, "cart_items", None), "simple N=1 path must never populate cart_items"
    assert any("Order Summary" in c or "confirmed" in c.lower() for c in captured)


# ---------------------------------------------------------------------------
# 2. Same-variant bulk — N=50, variant_mode="same" — flat-field path.
# ---------------------------------------------------------------------------

async def test_cart_same_variant_bulk(replay_http, replay_session, monkeypatch):
    """N=50, same color/size for all — one line item, cart_items stays empty."""
    capture_all(monkeypatch)
    phone, pnid = _phone("002"), _pnid("002")
    client, product = await _seed_bulk_variant_product(replay_session, phone=phone, pnid=pnid)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, selected_size=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s2.color.{ts}")
    await _msg(replay_http, phone, "L", pnid=pnid, wamid=f"wamid.s2.size.{ts}")
    await _msg(replay_http, phone, "50", pnid=pnid, wamid=f"wamid.s2.qty.{ts}")
    await _msg(replay_http, phone, "same", pnid=pnid, wamid=f"wamid.s2.mode.{ts}")
    await _msg(replay_http, phone, "Priya Mehta", pnid=pnid, wamid=f"wamid.s2.name.{ts}")
    await _msg(replay_http, phone, "9 Ring Road, Rajkot 360001", pnid=pnid, wamid=f"wamid.s2.addr.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s2.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s2.paid.{ts}")

    conv = await _conv_row(replay_session, conv_id)
    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.quantity == 50
    assert order.total_amount == 50 * order.unit_price
    items = await _line_items(replay_session, order.id)
    assert len(items) == 1
    assert items[0].quantity == 50
    assert items[0].variant_color == "Red"
    assert items[0].variant_size == "L"
    assert not getattr(conv, "cart_items", None), "'same' path must never populate cart_items"


# ---------------------------------------------------------------------------
# 3. Different-variant loop — N=5, two combos, mid-loop correction.
# ---------------------------------------------------------------------------

async def test_cart_different_variant_loop_with_correction(replay_http, replay_session, monkeypatch):
    """
    N=5, "different": item 1 = Red/L x2 (color/size already known from the
    leading slots), item 2 = Blue/M x2 — then corrected to x3 mid-loop
    ("actually make it 3 not 2") instead of creating a duplicate or being
    silently ignored. Final cart: Red/L x2 + Blue/M x3 = 5.
    """
    capture_all(monkeypatch)
    phone, pnid = _phone("003"), _pnid("003")
    client, product = await _seed_bulk_variant_product(replay_session, phone=phone, pnid=pnid)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, selected_size=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s3.color.{ts}")
    await _msg(replay_http, phone, "L", pnid=pnid, wamid=f"wamid.s3.size.{ts}")
    await _msg(replay_http, phone, "5", pnid=pnid, wamid=f"wamid.s3.qty.{ts}")
    await _msg(replay_http, phone, "different", pnid=pnid, wamid=f"wamid.s3.mode.{ts}")
    # Item 1 (Red/L) — only its qty is asked, color/size already known.
    await _msg(replay_http, phone, "2", pnid=pnid, wamid=f"wamid.s3.item1qty.{ts}")

    conv_mid = await _conv_row(replay_session, conv_id)
    assert conv_mid.cart_items and len(conv_mid.cart_items) == 1
    assert conv_mid.cart_items[0]["color"] == "Red"
    assert conv_mid.cart_items[0]["qty"] == 2

    # Item 2 — color, size, qty.
    await _msg(replay_http, phone, "Blue", pnid=pnid, wamid=f"wamid.s3.item2color.{ts}")
    await _msg(replay_http, phone, "M", pnid=pnid, wamid=f"wamid.s3.item2size.{ts}")
    await _msg(replay_http, phone, "2", pnid=pnid, wamid=f"wamid.s3.item2qty.{ts}")

    conv_before_fix = await _conv_row(replay_session, conv_id)
    assert len(conv_before_fix.cart_items) == 2
    assert conv_before_fix.cart_items[1]["qty"] == 2

    # Mid-loop correction — must adjust item 2 in place, not duplicate it.
    await _msg(replay_http, phone, "actually make it 3 not 2", pnid=pnid, wamid=f"wamid.s3.correct.{ts}")

    conv_after_fix = await _conv_row(replay_session, conv_id)
    assert len(conv_after_fix.cart_items) == 2, "correction must not create a duplicate line item"
    assert conv_after_fix.cart_items[1]["qty"] == 3
    assert sum(i["qty"] for i in conv_after_fix.cart_items) == 5

    await _msg(replay_http, phone, "Riya Patel", pnid=pnid, wamid=f"wamid.s3.name.{ts}")
    await _msg(replay_http, phone, "22 Station Road, Vadodara 390001", pnid=pnid, wamid=f"wamid.s3.addr.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s3.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s3.paid.{ts}")

    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.total_amount == 5 * order.unit_price
    items = await _line_items(replay_session, order.id)
    assert len(items) == 2
    by_color = {i.variant_color: i.quantity for i in items}
    assert by_color == {"Red": 2, "Blue": 3}


# ---------------------------------------------------------------------------
# 4. Different-variant batch — N=100, LLM breakdown parse (mocked).
# ---------------------------------------------------------------------------

async def test_cart_different_variant_batch(replay_http, replay_session, monkeypatch):
    """
    N=100, "different": item 1 (Red/L) takes 20, remaining 80 > threshold
    (10) triggers batch mode — a single free-text breakdown is parsed (LLM
    call mocked, since Groq is disabled in the replay harness) and, once its
    sum matches the remaining count exactly, commits without an extra
    confirmation turn.
    """
    capture_all(monkeypatch)
    phone, pnid = _phone("004"), _pnid("004")
    client, product = await _seed_bulk_variant_product(replay_session, phone=phone, pnid=pnid)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, selected_size=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s4.color.{ts}")
    await _msg(replay_http, phone, "L", pnid=pnid, wamid=f"wamid.s4.size.{ts}")
    await _msg(replay_http, phone, "100", pnid=pnid, wamid=f"wamid.s4.qty.{ts}")
    await _msg(replay_http, phone, "different", pnid=pnid, wamid=f"wamid.s4.mode.{ts}")
    await _msg(replay_http, phone, "20", pnid=pnid, wamid=f"wamid.s4.item1qty.{ts}")

    conv_mid = await _conv_row(replay_session, conv_id)
    assert conv_mid.cart_collection_mode == "batch"

    monkeypatch.setattr(
        "app.services.conversation_flow.extract_cart_breakdown",
        mock.AsyncMock(return_value={
            "items": [
                {"qty": 30, "color": "Blue", "size": "L"},
                {"qty": 50, "color": "Green", "size": "M"},
            ],
            "inferred_split": False,
        }),
    )
    await _msg(replay_http, phone, "30 blue L, 50 green M", pnid=pnid, wamid=f"wamid.s4.breakdown.{ts}")

    conv_after = await _conv_row(replay_session, conv_id)
    assert len(conv_after.cart_items) == 3
    assert sum(i["qty"] for i in conv_after.cart_items) == 100

    await _msg(replay_http, phone, "Karan Desai", pnid=pnid, wamid=f"wamid.s4.name.{ts}")
    await _msg(replay_http, phone, "5 Palace Road, Baroda 390002", pnid=pnid, wamid=f"wamid.s4.addr.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s4.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s4.paid.{ts}")

    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.total_amount == 100 * order.unit_price
    items = await _line_items(replay_session, order.id)
    assert len(items) == 3
    assert sum(i.quantity for i in items) == 100


# ---------------------------------------------------------------------------
# 5. Batch mismatch — sum != remaining → reprompt, never a silent commit.
# ---------------------------------------------------------------------------

async def test_cart_batch_mismatch_reprompts(replay_http, replay_session, monkeypatch):
    """A breakdown that doesn't sum to the remaining count must reprompt and
    leave cart_items untouched — never guess which number is right."""
    captured = capture_all(monkeypatch)
    phone, pnid = _phone("005"), _pnid("005")
    client, product = await _seed_bulk_variant_product(replay_session, phone=phone, pnid=pnid)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, selected_size=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s5.color.{ts}")
    await _msg(replay_http, phone, "L", pnid=pnid, wamid=f"wamid.s5.size.{ts}")
    await _msg(replay_http, phone, "100", pnid=pnid, wamid=f"wamid.s5.qty.{ts}")
    await _msg(replay_http, phone, "different", pnid=pnid, wamid=f"wamid.s5.mode.{ts}")
    await _msg(replay_http, phone, "20", pnid=pnid, wamid=f"wamid.s5.item1qty.{ts}")

    breakdown_mock = mock.AsyncMock(side_effect=[
        {"items": [{"qty": 30, "color": "Blue", "size": "L"}, {"qty": 40, "color": "Green", "size": "M"}],
         "inferred_split": False},  # sums to 70, remaining is 80 — mismatch
        {"items": [{"qty": 30, "color": "Blue", "size": "L"}, {"qty": 50, "color": "Green", "size": "M"}],
         "inferred_split": False},  # corrected — sums to 80
    ])
    monkeypatch.setattr("app.services.conversation_flow.extract_cart_breakdown", breakdown_mock)

    await _msg(replay_http, phone, "30 blue L, 40 green M", pnid=pnid, wamid=f"wamid.s5.breakdown1.{ts}")
    conv_after_mismatch = await _conv_row(replay_session, conv_id)
    assert len(conv_after_mismatch.cart_items) == 1, "mismatch must not commit anything"
    assert any("recheck" in c.lower() or "70" in c for c in captured), (
        f"expected a mismatch reprompt naming the wrong sum, got: {captured}"
    )

    await _msg(replay_http, phone, "30 blue L, 50 green M", pnid=pnid, wamid=f"wamid.s5.breakdown2.{ts}")
    conv_after_fix = await _conv_row(replay_session, conv_id)
    assert len(conv_after_fix.cart_items) == 3
    assert sum(i["qty"] for i in conv_after_fix.cart_items) == 100


# ---------------------------------------------------------------------------
# 6. Multi-value single message — "10 red and 10 pink" (the production bug).
# ---------------------------------------------------------------------------

async def test_cart_multi_value_single_message(replay_http, replay_session, monkeypatch):
    """
    The literal production bug (conv=60, Riya Sarees): a single message
    naming two colors with two quantities must land as TWO line items —
    neither is silently dropped.
    """
    capture_all(monkeypatch)
    phone, pnid = _phone("006"), _pnid("006")
    client, product = await _seed_bulk_variant_product(
        replay_session, phone=phone, pnid=pnid, color_only=True,
    )
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "10 red and 10 pink", pnid=pnid, wamid=f"wamid.s6.multi.{ts}")

    conv = await _conv_row(replay_session, conv_id)
    assert conv.cart_items is not None and len(conv.cart_items) == 2, (
        f"expected both colors captured as separate line items, got: {conv.cart_items!r}"
    )
    colors = {i["color"] for i in conv.cart_items}
    assert colors == {"Red", "Pink"}, f"a color was dropped: {conv.cart_items!r}"
    assert sum(i["qty"] for i in conv.cart_items) == 20
    assert conv.pending_order_quantity == 20
    assert conv.cart_variant_mode == "different"

    await _msg(replay_http, phone, "Sneha Rao", pnid=pnid, wamid=f"wamid.s6.name.{ts}")
    await _msg(replay_http, phone, "18 Lake View, Ahmedabad 380001", pnid=pnid, wamid=f"wamid.s6.addr.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s6.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s6.paid.{ts}")

    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.total_amount == 20 * order.unit_price
    items = await _line_items(replay_session, order.id)
    assert len(items) == 2
    by_color = {i.variant_color: i.quantity for i in items}
    assert by_color == {"Red": 10, "Pink": 10}, "neither color's units may be dropped"


# ---------------------------------------------------------------------------
# 7. Unprompted quantity mid-message — "100 pic leva che" (second bug fix).
# ---------------------------------------------------------------------------

async def test_cart_unprompted_quantity_at_color_slot(replay_http, replay_session, monkeypatch):
    """
    A bare quantity sent while the color question is pending (no color
    keyword in the message) must be captured, not silently discarded — the
    color question is still correctly re-asked next turn.
    """
    captured = capture_all(monkeypatch)
    phone, pnid = _phone("007"), _pnid("007")
    client, product = await _seed_bulk_variant_product(
        replay_session, phone=phone, pnid=pnid, color_only=True,
    )
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    # next_slot is "color" — this message has no color keyword, only a qty.
    await _msg(replay_http, phone, "100 pic leva che", pnid=pnid, wamid=f"wamid.s7.qty.{ts}")

    conv = await _conv_row(replay_session, conv_id)
    assert conv.pending_order_quantity == 100, (
        "bare quantity sent at the color slot must still be captured, not discarded"
    )
    assert conv.selected_color is None, "color itself was never given and must still be unset"
    assert any("colo" in c.lower() for c in captured[-1:]) or "colo" in captured[-1].lower(), (
        f"color question must still be (re-)asked next, got: {captured[-1]!r}"
    )

    # Answering color now must NOT re-ask quantity — it was already captured.
    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s7.color.{ts}")
    conv2 = await _conv_row(replay_session, conv_id)
    assert conv2.selected_color == "Red"
    assert conv2.pending_order_quantity == 100


# ---------------------------------------------------------------------------
# 8. Post-payment success message must itemize the cart, not collapse it.
#
# Regression for the bug where the pre-payment cart_confirm summary correctly
# iterated cart_items, but the post-payment "order placed" message still read
# the legacy flat slots (pending_order_quantity/selected_color/selected_size)
# — which only ever hold line item 1's data — silently dropping every other
# line item from the success message the customer actually sees after paying.
# ---------------------------------------------------------------------------

async def test_cart_post_payment_message_matches_pre_payment_summary(
    replay_http, replay_session, monkeypatch
):
    """
    Two-item cart (Red/L x5 + Blue/M x5, conv=60-style): both the pre-payment
    cart_confirm summary and the post-payment success message must list BOTH
    line items with correct per-line quantities/totals and the same grand
    total — neither may collapse to a single merged line.
    """
    captured = capture_all(monkeypatch)
    phone, pnid = _phone("008"), _pnid("008")
    client, product = await _seed_bulk_variant_product(replay_session, phone=phone, pnid=pnid)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, stage="order_collection",
        selected_color=None, selected_size=None, pending_order_quantity=None,
        customer_name=None, delivery_address=None, payment_method=None,
    )
    ts = int(time.time())

    await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.s8.color.{ts}")
    await _msg(replay_http, phone, "L", pnid=pnid, wamid=f"wamid.s8.size.{ts}")
    await _msg(replay_http, phone, "10", pnid=pnid, wamid=f"wamid.s8.qty.{ts}")
    await _msg(replay_http, phone, "different", pnid=pnid, wamid=f"wamid.s8.mode.{ts}")
    await _msg(replay_http, phone, "5", pnid=pnid, wamid=f"wamid.s8.item1qty.{ts}")
    await _msg(replay_http, phone, "Blue", pnid=pnid, wamid=f"wamid.s8.item2color.{ts}")
    await _msg(replay_http, phone, "M", pnid=pnid, wamid=f"wamid.s8.item2size.{ts}")
    await _msg(replay_http, phone, "5", pnid=pnid, wamid=f"wamid.s8.item2qty.{ts}")
    await _msg(replay_http, phone, "Karan Mehta", pnid=pnid, wamid=f"wamid.s8.name.{ts}")
    # This turn completes every slot (UPI auto-fills for this UPI-only
    # client) and renders the pre-payment cart_confirm summary immediately —
    # there is no separate "yes" needed to trigger it.
    await _msg(replay_http, phone, "9 Ellis Bridge, Ahmedabad 380006", pnid=pnid, wamid=f"wamid.s8.addr.{ts}")
    pre_payment_summary = captured[-1]
    assert "Red" in pre_payment_summary and "Blue" in pre_payment_summary, (
        f"pre-payment summary must list both colors: {pre_payment_summary!r}"
    )

    conv_before_pay = await _conv_row(replay_session, conv_id)
    assert len(conv_before_pay.cart_items) == 2
    unit_price = conv_before_pay.cart_items[0]["unit_price"]
    expected_total = 10 * unit_price

    # "yes" confirms the summary and advances to the payment prompt.
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s8.yes.{ts}")
    assert "× 5" in pre_payment_summary or "x 5" in pre_payment_summary.lower(), (
        f"pre-payment summary must show per-line qty 5, not merged: {pre_payment_summary!r}"
    )

    # "paid" renders the post-payment success message — the one this bug hit.
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s8.paid.{ts}")
    post_payment_message = captured[-1]

    assert "Red" in post_payment_message, (
        f"post-payment message dropped the Red line item: {post_payment_message!r}"
    )
    assert "Blue" in post_payment_message, (
        f"post-payment message dropped the Blue line item — this is the exact bug "
        f"(conv=60, ORD-2026-0024) where the second variant silently disappeared: "
        f"{post_payment_message!r}"
    )
    # Neither line item's own qty (5) may have been merged into a single ×10 line.
    assert "× 10" not in post_payment_message and "x 10" not in post_payment_message.lower(), (
        f"post-payment message merged both line items' quantities into one: {post_payment_message!r}"
    )

    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "paid"
    assert order.total_amount == expected_total

    # DB integrity check (bug report item 3) — both line items, not collapsed.
    items = await _line_items(replay_session, order.id)
    assert len(items) == 2, f"expected 2 OrderLineItem rows, got {len(items)}: {items!r}"
    by_color = {i.variant_color: i.quantity for i in items}
    assert by_color == {"Red": 5, "Blue": 5}, (
        f"DB order record is corrupted, not just the message: {by_color!r}"
    )
    assert sum(i.subtotal for i in items) == expected_total
