"""
End-to-end replay of the numeric-size variant flow that broke in production.

Live failure (conv=60, "Traditional choli", sizes 38/3XL/40/L/M/S/XL/XXL):
  1. customer answers the SIZE question with "40" (a real size)
  2. "40" is also swallowed as the QUANTITY, so the quantity question is skipped
  3. summary quotes 40 pieces = ₹159,960
  4. order creation is blocked (qty 40 > stock 10) — but the customer is STILL
     told "Please pay ₹159,960" for an order that was never created

These tests drive the real webhook against real Postgres. The conversation is
primed straight into order_collection (same approach as test_extended.py) so
the assertions target slot-filling, not the browse/pin phase.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.replay.conftest import WA_PHONE_NUMBER_ID
from tests.replay.helpers import capture_all, send_message

pytestmark = pytest.mark.asyncio


async def _seed_numeric_size_product(session, phone: str, *, stock_40: int = 10):
    """UPI-only client + variant product whose sizes include numeric ones."""
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Numeric Size Store",
        email=f"numsize_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=WA_PHONE_NUMBER_ID,
        accepts_cod=False,
        hashed_password="x",
        upi_id="numsize@upi",
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="Traditional choli",
        sku="PR17761",
        price=3999.0,
        stock=stock_40 + 5,
        is_active=True,
        has_variants=True,
    )
    session.add(product)
    await session.flush()

    session.add_all([
        ProductVariant(
            product_id=product.id, client_id=client.id, color="Red", size="40",
            stock=stock_40, is_active=True,
        ),
        ProductVariant(
            product_id=product.id, client_id=client.id, color="Red", size="38",
            stock=5, is_active=True,
        ),
    ])
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


async def _prime(session, *, phone, product, **slots):
    """Create a Conversation already in order_collection with the product pinned."""
    from app.models.conversation import Conversation

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage="order_collection",
        pending_product_sku=product.sku,
        summary_shown=False,
        **slots,
    )
    session.add(conv)
    await session.commit()
    return conv.id


async def _reload(session, conv_id: int):
    """Re-read the conversation row, bypassing the identity map."""
    from app.models.conversation import Conversation

    return (await session.execute(
        select(Conversation)
        .where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _orders(session, conv_id: int):
    from app.models.order import Order

    return list((await session.execute(
        select(Order)
        .where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def test_numeric_size_answer_does_not_skip_quantity_question(
    replay_http, replay_session, monkeypatch
):
    """Answering "40" to the size question must still leave quantity to collect."""
    phone = "919900000601"
    _client, product = await _seed_numeric_size_product(replay_session, phone)
    conv_id = await _prime(replay_session, phone=phone, product=product, selected_color="Red")
    captured = capture_all(monkeypatch)

    await send_message(replay_http, phone, "40")

    conv = await _reload(replay_session, conv_id)
    assert conv.selected_size == "40", "size should be filled from the numeric token"
    assert conv.pending_order_quantity is None, (
        "the size token was double-counted as quantity — the bug this test guards"
    )
    assert not any("Order Summary" in (t or "") for t in captured), (
        f"summary shown before quantity was collected: {captured}"
    )


async def test_numeric_size_then_quantity_orders_correct_amount(
    replay_http, replay_session, monkeypatch
):
    """size "40" then quantity "2" yields an order for 2 pieces, not 40."""
    phone = "919900000602"
    _client, product = await _seed_numeric_size_product(replay_session, phone)
    conv_id = await _prime(
        replay_session, phone=phone, product=product,
        selected_color="Red",
        customer_name="Amit",
        delivery_address="702 Somerset, Ahmedabad 380015",
        mobile_number="9876543210",
    )
    capture_all(monkeypatch)

    await send_message(replay_http, phone, "40")   # size
    await send_message(replay_http, phone, "2")    # quantity

    conv = await _reload(replay_session, conv_id)
    assert conv.selected_size == "40"
    assert conv.pending_order_quantity == 2, (
        f"expected qty 2, got {conv.pending_order_quantity}"
    )

    await send_message(replay_http, phone, "yes")  # confirm summary

    orders = await _orders(replay_session, conv_id)
    assert len(orders) == 1, f"expected exactly one order, got {len(orders)}"
    assert orders[0].quantity == 2
    assert orders[0].variant_size == "40"
    assert orders[0].variant_color == "Red"


async def test_quantity_over_stock_never_asks_for_payment(
    replay_http, replay_session, monkeypatch
):
    """
    Quantity above stock must never produce payment instructions — the live
    bug quoted ₹159,960 for an order that was blocked and never created.
    """
    phone = "919900000603"
    _client, product = await _seed_numeric_size_product(replay_session, phone, stock_40=10)
    conv_id = await _prime(
        replay_session, phone=phone, product=product,
        selected_color="Red", selected_size="40",
        customer_name="Amit",
        delivery_address="702 Somerset, Ahmedabad 380015",
        mobile_number="9876543210",
    )
    captured = capture_all(monkeypatch)

    await send_message(replay_http, phone, "25")   # qty > stock (10)
    await send_message(replay_http, phone, "yes")  # try to confirm

    joined = "\n---\n".join(t or "" for t in captured)
    orders = await _orders(replay_session, conv_id)

    assert not orders, f"order created above stock: {[o.quantity for o in orders]}"
    assert "PAID" not in joined.upper(), (
        f"payment instructions sent with no order created:\n{joined}"
    )
    assert "10" in joined, f"customer was never told the real stock limit:\n{joined}"


async def test_invalid_size_gets_explicit_rejection(
    replay_http, replay_session, monkeypatch
):
    """An unavailable size is named as unavailable, not silently re-asked."""
    phone = "919900000604"
    _client, product = await _seed_numeric_size_product(replay_session, phone)
    conv_id = await _prime(replay_session, phone=phone, product=product, selected_color="Red")
    captured = capture_all(monkeypatch)

    await send_message(replay_http, phone, "45")   # not a stocked size

    last = (captured[-1] or "").lower()
    assert "45" in last and (
        "don't have" in last or "not available" in last or "nathi" in last
    ), f"invalid size was not explained to the customer: {captured[-1]!r}"

    conv = await _reload(replay_session, conv_id)
    assert conv.selected_size is None
    assert conv.pending_order_quantity is None
