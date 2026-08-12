"""
Replay regression tests for WhatsApp button corner cases.

All tests run against a real Postgres DB (replay_ci_test), using the
module-scoped fixture that creates the schema once per run.

Pattern: pre-prime the conversation with all slots filled via _prime_conv
(avoids the full slot-collection HTTP round-trips and classify_user_intent
mock issues), then assert DB end-state after specific button taps.

Scenarios
---------
1. confirm → cancel  → order.status == 'cancelled', stock unchanged
2. confirm → cancel  → stale 'paid' tap → NO order paid, friendly reply
3. double-tap confirm (two wamids)  → exactly ONE order row
4. double-tap 'paid' → order paid once, stock deducted once
5. expired-nonce button tap → no order, expiry message returned
"""

from __future__ import annotations

import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import send_button, send_message


pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phone(suffix: str) -> str:
    return f"91910000{suffix}"


def _pnid(suffix: str) -> str:
    """Unique WA phone_number_id per test to avoid client-lookup collisions."""
    return f"222{suffix}"


async def _seed(session: AsyncSession, *, suffix: str, **kwargs):
    return await seed_client_and_product(
        session,
        phone=_phone(suffix),
        wa_phone_number_id=_pnid(suffix),
        **kwargs,
    )


async def _prime_conv(
    session: AsyncSession,
    *,
    phone: str,
    product,
    stage: str,
    **slots,
):
    """Insert a conversation with all required slots pre-filled."""
    from app.models.conversation import Conversation

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage=stage,
        pending_product_sku=product.sku,
        summary_shown=slots.pop("summary_shown", stage == "awaiting_final_confirmation"),
        **slots,
    )
    session.add(conv)
    await session.commit()
    return conv.id


async def _get_orders(session: AsyncSession, conv_id: int):
    from app.models.order import Order
    r = await session.execute(
        select(Order)
        .where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


async def _get_stock(session: AsyncSession, product_id: int) -> int:
    from app.models.product import Product
    r = await session.execute(
        select(Product)
        .where(Product.id == product_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one().stock


async def _btn(http_client, phone: str, btn_id: str, btn_title: str, *, pnid: str, wamid: str | None = None):
    """POST a button reply through the replay HTTP client."""
    return await send_button(http_client, phone, btn_id, btn_title, wamid=wamid, phone_number_id=pnid)


async def _msg(http_client, phone: str, text: str, *, pnid: str, wamid: str | None = None):
    return await send_message(http_client, phone, text, wamid=wamid, phone_number_id=pnid)


# ---------------------------------------------------------------------------
# Test 1 — confirm → cancel: order row set to 'cancelled', stock unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_then_cancel_order_cancelled(replay_http, replay_session):
    """
    UPI order: customer confirms the summary (order created as pending_payment),
    then taps Cancel.  The order must be set to 'cancelled' and stock must stay
    unchanged (stock is only deducted in mark_order_paid, not at creation).
    """
    suffix = "B001"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await _seed(
        replay_session, suffix=suffix,
        product_sku="BC001",
        product_name="Cancel Test Kurta",
        price=500.0,
        stock=10,
        # UPI: order stays pending_payment after confirm; cancel fires before 'paid'
        payment_method="UPI",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Priya Sharma",
        delivery_address="45 Link Road, Mumbai",
        pending_order_quantity=2,
        payment_method="UPI",
    )

    # Confirm → order created as pending_payment
    resp = await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay",
                      pnid=pnid, wamid=f"wamid.bc001.confirm.{time.time_ns()}")
    assert resp.status_code == 200

    # Verify order exists and is pending_payment before cancel
    orders_before = await _get_orders(replay_session, conv_id)
    assert orders_before, "Order must be created after confirm"
    assert orders_before[0].status == "pending_payment"

    # Cancel → order must become 'cancelled'
    resp = await _btn(replay_http, phone, "cancel_order", "Cancel",
                      pnid=pnid, wamid=f"wamid.bc001.cancel.{time.time_ns()}")
    assert resp.status_code == 200

    orders_after = await _get_orders(replay_session, conv_id)
    assert orders_after, "Order row must still exist after cancel"
    for o in orders_after:
        assert o.status == "cancelled", (
            f"Expected 'cancelled', got '{o.status}' for {o.order_number}"
        )
        assert not o.stock_deducted, "Stock must NOT be deducted on a cancelled order"

    # Stock unchanged — 10 in, 10 out
    assert await _get_stock(replay_session, product_id) == 10, "Stock must not change on cancel"


# ---------------------------------------------------------------------------
# Test 2 — confirm → cancel → stale 'paid' tap → no order marked paid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_paid_after_cancel_never_marks_paid(replay_http, replay_session):
    """
    After confirm+cancel, tapping the old "I've Paid" button must:
    - NOT mark any order paid
    - Reply with a friendly cancellation message (not "Which item…")
    """
    suffix = "B002"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await _seed(
        replay_session, suffix=suffix,
        product_sku="BC002",
        product_name="Stale Paid Kurta",
        price=600.0,
        stock=5,
        payment_method="UPI",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Arjun Patel",
        delivery_address="7 Ring Road, Ahmedabad",
        pending_order_quantity=1,
        payment_method="UPI",
    )

    # Confirm → pending_payment order created
    await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay",
               pnid=pnid, wamid=f"wamid.bc002.confirm.{time.time_ns()}")
    # Cancel → order becomes 'cancelled'
    await _btn(replay_http, phone, "cancel_order", "Cancel",
               pnid=pnid, wamid=f"wamid.bc002.cancel.{time.time_ns()}")

    # Stale 'paid' tap — capture the reply
    sent_texts: list[str] = []

    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ):
        resp = await _btn(replay_http, phone, "paid_done", "I've Paid",
                          pnid=pnid, wamid=f"wamid.bc002.stale_paid.{time.time_ns()}")
    assert resp.status_code == 200

    # Assert: no order is 'paid'
    for o in await _get_orders(replay_session, conv_id):
        assert o.status != "paid", f"Order {o.order_number} must not be paid after stale tap"
        assert not o.stock_deducted, "Stock must not be deducted by a stale paid tap"

    # Assert: friendly reply returned (not the AI browse fallback)
    assert sent_texts, "Expected a reply text to be sent"
    reply_lower = sent_texts[-1].lower()
    assert "cancel" in reply_lower or "new" in reply_lower, (
        f"Expected 'cancelled/new order' reply, got: {sent_texts[-1]!r}"
    )
    assert "which item" not in reply_lower, (
        "Reply must not fall through to AI browse path"
    )


# ---------------------------------------------------------------------------
# Test 3 — double-tap confirm (two distinct wamids) → exactly ONE order row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_double_tap_confirm_creates_one_order(replay_http, replay_session):
    """Two confirm taps with different wamids must produce exactly one order."""
    suffix = "B003"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await _seed(
        replay_session, suffix=suffix,
        product_sku="BC003",
        product_name="Double Confirm Kurta",
        price=700.0,
        stock=10,
        payment_method="UPI",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Meera Singh",
        delivery_address="22 Banjara Hills, Hyderabad",
        pending_order_quantity=1,
        payment_method="UPI",
    )

    # First confirm tap
    await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay",
               pnid=pnid, wamid=f"wamid.bc003.confirm_a.{time.time_ns()}")
    # Second confirm tap (different wamid — simulates scroll-back double-tap)
    await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay",
               pnid=pnid, wamid=f"wamid.bc003.confirm_b.{time.time_ns() + 1}")

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, (
        f"Expected exactly 1 order, got {len(orders)}: "
        f"{[o.order_number for o in orders]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — double-tap 'paid' → order paid once, stock deducted once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_double_tap_paid_idempotent(replay_http, replay_session):
    """Two 'paid' taps must leave the order paid exactly once, stock deducted once."""
    suffix = "B004"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await _seed(
        replay_session, suffix=suffix,
        product_sku="BC004",
        product_name="Double Paid Kurta",
        price=800.0,
        stock=10,
        payment_method="UPI",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Kavita Rao",
        delivery_address="5 Koramangala, Bangalore",
        pending_order_quantity=2,
        payment_method="UPI",
    )

    # Confirm → order created as pending_payment (UPI)
    await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay",
               pnid=pnid, wamid=f"wamid.bc004.confirm.{time.time_ns()}")

    # First paid tap → order transitions to 'paid', stock deducted
    await _btn(replay_http, phone, "paid_done", "I've Paid",
               pnid=pnid, wamid=f"wamid.bc004.paid_a.{time.time_ns()}")

    # Second paid tap → must be a no-op (idempotent)
    await _btn(replay_http, phone, "paid_done", "I've Paid",
               pnid=pnid, wamid=f"wamid.bc004.paid_b.{time.time_ns() + 1}")

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Expected 1 order, got {len(orders)}"

    o = orders[0]
    assert o.status == "paid", f"Expected status='paid', got '{o.status}'"
    assert o.stock_deducted is True, "stock_deducted must be True"

    # Stock deducted exactly once: 10 - 2 (qty=2) = 8
    final_stock = await _get_stock(replay_session, product_id)
    assert final_stock == 8, (
        f"Stock should be 8 after deducting qty=2 once, got {final_stock}"
    )


# ---------------------------------------------------------------------------
# Test 5 — expired-nonce button tap → no state change, expiry message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_nonce_button_rejected(replay_http, replay_session):
    """
    A button tap carrying an outdated nonce must be rejected — no order created,
    no state change, friendly 'That option has expired' reply returned.
    """
    suffix = "B005"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await _seed(
        replay_session, suffix=suffix,
        product_sku="BC005",
        product_name="Nonce Test Kurta",
        price=900.0,
        stock=10,
        payment_method="UPI",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Sunita Verma",
        delivery_address="9 Civil Lines, Delhi",
        pending_order_quantity=1,
        payment_method="UPI",
        # Pre-set a known nonce so we can craft a button with a DIFFERENT one.
        current_button_nonce="aabbccdd",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ):
        # Button ID encodes a WRONG nonce ("deadbeef" ≠ stored "aabbccdd")
        stale_btn_id = f"confirm_pay~{conv_id}~deadbeef"
        resp = await _btn(replay_http, phone, stale_btn_id, "Confirm & Pay",
                          pnid=pnid, wamid=f"wamid.bc005.stale.{time.time_ns()}")
    assert resp.status_code == 200

    # Assert: no order was created
    orders = await _get_orders(replay_session, conv_id)
    assert not orders, (
        f"No order must be created when nonce is invalid; got {[o.order_number for o in orders]}"
    )

    # Assert: expiry message was sent
    assert sent_texts, "Expected a reply on expired-nonce tap"
    assert "expired" in sent_texts[-1].lower(), (
        f"Expected expiry message, got: {sent_texts[-1]!r}"
    )
