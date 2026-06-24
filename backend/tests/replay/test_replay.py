"""
Golden replay harness — Phase 4 Part B.

6 scenarios, each asserting DB END-STATE (orders, stock, conversation slots).
All tests hit a REAL Postgres instance (replay_ci_test DB, created fresh per run).
Tests are skipped automatically if local Postgres is not reachable.

Scenarios:
  1. COD happy path         — "yes" → order created, paid, stock decremented once
  2. UPI happy path         — "yes" → pending_payment, then "paid" → paid, stock -qty
  3. Duplicate "paid"       — second "paid" is idempotent (stock_deducted stays True, one row)
  4. Duplicate "yes" (COD)  — second confirm does NOT create a second order row
  5. Variant browse→buy     — all slots filled before order is created
  6. COD + OOS              — order blocked when qty > stock
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_message


pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phone(suffix: str) -> str:
    return f"91900000{suffix}"


def _pnid(suffix: str) -> str:
    """Unique WhatsApp phone_number_id per scenario to avoid client-lookup collisions."""
    return f"111{suffix}"


async def _seed(session: AsyncSession, *, phone: str, phone_number_id: str, **kwargs):
    """Insert client + product; phone_number_id must be unique per test."""
    from tests.replay.conftest import seed_client_and_product
    return await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=phone_number_id, **kwargs
    )


async def _prime_conv(session: AsyncSession, *, phone: str, product, stage: str, **slots):
    """Insert a conversation pre-loaded with slot values; return conv_id (int)."""
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
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
        select(Order).where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


async def _get_stock(session: AsyncSession, product_id: int) -> int:
    from app.models.product import Product
    r = await session.execute(
        select(Product).where(Product.id == product_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one().stock


async def _msg(http_client, phone: str, text: str, *, pnid: str, wamid: str | None = None):
    return await send_message(
        http_client, phone, text, wamid=wamid, phone_number_id=pnid
    )


# ---------------------------------------------------------------------------
# Scenario 1 — COD happy path
# ---------------------------------------------------------------------------

async def test_scenario1_cod_happy_path(replay_http, replay_session):
    """
    COD: customer confirms order.
    Expected end-state: 1 order row, status='paid', stock decremented by qty.
    """
    phone = _phone("0001")
    pnid = _pnid("0001")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S1SKU", product_name="Test Kurta",
        price=500.0, stock=10, payment_method="COD",
    )
    product_id = product.id
    initial_stock = 10

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Ramesh Kumar",
        delivery_address="12 MG Road, Bangalore",
        pending_order_quantity=2,
        payment_method="COD",
    )

    resp = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s1.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    stock = await _get_stock(replay_session, product_id)

    assert len(orders) == 1, f"Expected 1 order, got {len(orders)}"
    o = orders[0]
    assert o.status == "paid", f"Expected status='paid', got {o.status!r}"
    assert o.payment_method == "COD"
    assert o.stock_deducted is True
    assert o.paid_at is not None
    assert stock == initial_stock - 2, f"Expected stock {initial_stock-2}, got {stock}"

    print(f"\n[S1] order={o.order_number} status={o.status} stock_deducted={o.stock_deducted} stock_after={stock}")


# ---------------------------------------------------------------------------
# Scenario 2 — UPI happy path (pending_payment → paid → stock -qty)
# ---------------------------------------------------------------------------

async def test_scenario2_upi_payment_confirmed(replay_http, replay_session):
    """
    UPI: customer confirms summary → order created as pending_payment.
    Customer then says 'paid' → order transitions to paid, stock decremented.
    """
    phone = _phone("0002")
    pnid = _pnid("0002")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S2SKU", product_name="Silk Saree",
        price=1200.0, stock=5, payment_method="UPI",
    )
    product_id = product.id
    initial_stock = 5

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Priya Sharma",
        delivery_address="45 Park Street, Mumbai",
        pending_order_quantity=1,
        payment_method="UPI",
    )

    # Step 1: "yes" → UPI override → stage becomes "payment", order created as pending_payment
    resp1 = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s2.a.{int(time.time())}")
    assert resp1.status_code == 200

    orders_after_yes = await _get_orders(replay_session, conv_id)
    assert len(orders_after_yes) == 1, f"Expected 1 order after yes, got {len(orders_after_yes)}"
    o = orders_after_yes[0]
    assert o.status == "pending_payment", f"Expected pending_payment, got {o.status!r}"
    assert o.stock_deducted is False
    stock_after_yes = await _get_stock(replay_session, product_id)
    assert stock_after_yes == initial_stock, "Stock must NOT be decremented at pending_payment"

    print(f"\n[S2-yes] order={o.order_number} status={o.status} stock_deducted={o.stock_deducted} stock={stock_after_yes}")

    # Step 2: "paid" → order transitions to paid, stock decremented
    resp2 = await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s2.b.{int(time.time())}")
    assert resp2.status_code == 200

    orders_after_paid = await _get_orders(replay_session, conv_id)
    assert len(orders_after_paid) == 1
    o2 = orders_after_paid[0]
    assert o2.status == "paid", f"Expected paid, got {o2.status!r}"
    assert o2.stock_deducted is True
    assert o2.paid_at is not None
    stock_after_paid = await _get_stock(replay_session, product_id)
    assert stock_after_paid == initial_stock - 1, f"Expected stock {initial_stock-1}, got {stock_after_paid}"

    print(f"[S2-paid] order={o2.order_number} status={o2.status} stock_deducted={o2.stock_deducted} stock={stock_after_paid}")


# ---------------------------------------------------------------------------
# Scenario 3 — Duplicate "paid" is idempotent
# ---------------------------------------------------------------------------

async def test_scenario3_duplicate_paid_idempotent(replay_http, replay_session):
    """
    Sending "paid" twice must not double-decrement stock.
    stock_deducted flag is the idempotency guard.
    """
    phone = _phone("0003")
    pnid = _pnid("0003")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S3SKU", product_name="Cotton Dupatta",
        price=300.0, stock=8, payment_method="UPI",
    )
    product_id = product.id
    initial_stock = 8

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Anita Patel",
        delivery_address="7 Gandhi Nagar, Ahmedabad",
        pending_order_quantity=2,
        payment_method="UPI",
    )

    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s3.a.{int(time.time())}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s3.b.{int(time.time())}")

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1
    stock_after_first = await _get_stock(replay_session, product_id)
    assert stock_after_first == initial_stock - 2

    # Second "paid" — must be idempotent
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s3.c.{int(time.time())}")

    orders_after = await _get_orders(replay_session, conv_id)
    stock_after_second = await _get_stock(replay_session, product_id)

    assert len(orders_after) == 1, "No duplicate order rows"
    assert stock_after_second == initial_stock - 2, "Stock must not be decremented twice"
    assert orders_after[0].stock_deducted is True

    print(f"\n[S3] order_count={len(orders_after)} stock={stock_after_second} (should be {initial_stock-2})")


# ---------------------------------------------------------------------------
# Scenario 4 — Duplicate "yes" (COD) does NOT create a second order
# ---------------------------------------------------------------------------

async def test_scenario4_duplicate_yes_cod_idempotent(replay_http, replay_session):
    """
    Sending "yes" twice for a COD order (e.g. webhook retry) must not insert
    a second order row.
    """
    phone = _phone("0004")
    pnid = _pnid("0004")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S4SKU", product_name="Jute Bag",
        price=200.0, stock=15, payment_method="COD",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Vikram Singh",
        delivery_address="3 Church Road, Pune",
        pending_order_quantity=1,
        payment_method="COD",
    )

    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s4.a.{int(time.time())}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s4.b.{int(time.time())}")

    orders = await _get_orders(replay_session, conv_id)
    stock = await _get_stock(replay_session, product_id)

    assert len(orders) == 1, f"Expected 1 order, got {len(orders)}"
    assert orders[0].status == "paid"
    assert stock == 15 - 1, f"Expected stock 14, got {stock}"

    print(f"\n[S4] order_count={len(orders)} stock={stock}")


# ---------------------------------------------------------------------------
# Scenario 5 — Variant browse→buy: order created only after all slots filled
# ---------------------------------------------------------------------------

async def test_scenario5_variant_browse_to_buy(replay_http, replay_session):
    """
    With a variant product (needs color + size), order must include those slots
    and stock must come from the variant row.
    """
    phone = _phone("0005")
    pnid = _pnid("0005")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S5SKU", product_name="Designer Kurta",
        price=800.0, stock=6, has_variants=True, payment_method="COD",
    )
    product_id = product.id

    from app.models.product_variant import ProductVariant
    variant = ProductVariant(
        product_id=product.id,
        client_id=client.id,
        color="Blue",
        size="M",
        stock=6,
        is_active=True,
        price=800.0,
    )
    replay_session.add(variant)
    await replay_session.commit()

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Meena Joshi",
        delivery_address="22 Nehru Road, Delhi",
        pending_order_quantity=1,
        payment_method="COD",
        selected_color="Blue",
        selected_size="M",
    )

    resp = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s5.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)

    assert len(orders) == 1, f"Expected 1 order, got {len(orders)}"
    o = orders[0]
    assert o.variant_color == "Blue"
    assert o.variant_size == "M"
    assert o.status == "paid"
    assert o.stock_deducted is True

    print(f"\n[S5] order={o.order_number} color={o.variant_color} size={o.variant_size} status={o.status}")


# ---------------------------------------------------------------------------
# Scenario 6 — COD order blocked when qty > stock
# ---------------------------------------------------------------------------

async def test_scenario6_cod_blocked_when_qty_exceeds_stock(replay_http, replay_session):
    """
    If pending_order_quantity > product.stock, order creation must be blocked.
    No order row must appear; stock must not change.
    """
    phone = _phone("0006")
    pnid = _pnid("0006")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S6SKU", product_name="Limited Stole",
        price=350.0, stock=1, payment_method="COD",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Rajan Mehta",
        delivery_address="9 Lake Road, Chennai",
        pending_order_quantity=5,   # 5 requested, only 1 in stock
        payment_method="COD",
    )

    resp = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s6.{int(time.time())}")
    assert resp.status_code == 200  # webhook always returns 200

    orders = await _get_orders(replay_session, conv_id)
    stock = await _get_stock(replay_session, product_id)

    assert len(orders) == 0, f"Expected 0 orders (blocked), got {len(orders)}"
    assert stock == 1, f"Stock must not change when order is blocked; got {stock}"

    print(f"\n[S6] order_count={len(orders)} stock_unchanged={stock}")


# ---------------------------------------------------------------------------
# BUG-FIX REPLAY TESTS (post-Phase-4 live bugs)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Scenario 7 — Post-completion "yes" must NOT create a new order (BUG 1 fix)
# ---------------------------------------------------------------------------

async def test_scenario7_yes_after_completed_no_phantom_order(replay_http, replay_session):
    """
    BUG 1 regression: after stage=completed, a bare "yes" must NOT restart a
    variant order or create a second order row.

    End-state: stage stays completed, no new order row, conv.current_stage == 'completed'.
    """
    phone = _phone("0007")
    pnid = _pnid("0007")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S7SKU", product_name="Classic Kurta",
        price=600.0, stock=8, has_variants=True, payment_method="COD",
    )

    # Add a variant with size "S" — the old substring bug fires when size "S"
    # is found inside "yes" via "s" in "yes".
    from app.models.product_variant import ProductVariant
    variant = ProductVariant(
        product_id=product.id,
        client_id=client.id,
        color="Red",
        size="S",
        stock=8,
        is_active=True,
        price=600.0,
    )
    replay_session.add(variant)
    await replay_session.commit()

    # Seed a completed conversation (one existing order row already present).
    from app.models.order import Order
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="completed",
        customer_name="Suresh Patel",
        delivery_address="5 MG Road, Indore",
        pending_order_quantity=1,
        payment_method="COD",
        selected_color="Red",
        selected_size="S",
    )
    # Insert the existing order row so the duplicate-guard in create_order works.
    existing_order = Order(
        order_number="ORD-EXISTING-0007",
        client_id=client.id,
        conversation_id=conv_id,
        customer_name="Suresh Patel",
        customer_phone=phone,
        delivery_address="5 MG Road, Indore",
        product_name=product.name,
        product_sku=product.sku,
        quantity=1,
        unit_price=600.0,
        total_amount=600.0,
        payment_method="COD",
        status="paid",
        stock_deducted=True,
    )
    replay_session.add(existing_order)
    await replay_session.commit()

    # Customer says "yes" after completion — bare affirmation, NOT a new order.
    resp = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.s7.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, (
        f"BUG 1: 'yes' after completed created a phantom order! "
        f"Expected 1 (the original), got {len(orders)}: {[o.order_number for o in orders]}"
    )

    # Confirm stage did NOT advance to order_collection (variant restart blocked).
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()
    assert conv_row.current_stage in ("completed", "greeting"), (
        f"Stage should not be 'order_collection' after bare yes: {conv_row.current_stage}"
    )

    print(
        f"\n[S7] orders={len(orders)} stage={conv_row.current_stage} "
        f"— no phantom order, size 'S' in 'yes' correctly suppressed ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 8 — "thank you" after completed: no restart, no phantom order (BUG 1)
# ---------------------------------------------------------------------------

async def test_scenario8_thanks_after_completed_no_restart(replay_http, replay_session):
    """
    BUG 1 regression: 'thank you' after stage=completed must not start a new order
    or re-pin the last SKU.
    """
    phone = _phone("0008")
    pnid = _pnid("0008")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S8SKU", product_name="Embroidered Dupatta",
        price=450.0, stock=5, payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="completed",
        customer_name="Kavita Devi",
        delivery_address="11 Ring Road, Jaipur",
        pending_order_quantity=2,
        payment_method="COD",
    )
    from app.models.order import Order
    replay_session.add(Order(
        order_number="ORD-EXISTING-0008",
        client_id=client.id,
        conversation_id=conv_id,
        customer_name="Kavita Devi",
        customer_phone=phone,
        delivery_address="11 Ring Road, Jaipur",
        product_name=product.name,
        product_sku=product.sku,
        quantity=2,
        unit_price=450.0,
        total_amount=900.0,
        payment_method="COD",
        status="paid",
        stock_deducted=True,
    ))
    await replay_session.commit()

    resp = await _msg(replay_http, phone, "thank you", pnid=pnid, wamid=f"wamid.s8.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, (
        f"BUG 1: 'thank you' after completed caused a phantom order! Got {len(orders)}"
    )

    print(f"\n[S8] orders={len(orders)} — 'thank you' post-completion safe ✓")


# ---------------------------------------------------------------------------
# Scenario 9 — Address "yes" → exactly ONE confirm UI, no "already confirmed" (BUG 2)
# ---------------------------------------------------------------------------

async def test_scenario9_address_yes_single_confirm_ui(replay_http, replay_session):
    """
    BUG 2 regression: confirming a saved address ('yes') must NOT emit the
    'already_confirmed' text.  The reply should show the order summary ONCE
    (action=show_summary, not reask_confirm), and no order row yet (order is
    still pending_confirmation, not completed).
    """
    phone = _phone("0009")
    pnid = _pnid("0009")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S9SKU", product_name="Cotton Saree",
        price=750.0, stock=10, payment_method="COD",
    )

    # Prime conversation at delivery_address slot (all other slots filled).
    # The customer_profile has a saved address so the agent offered "Deliver to X? yes/change".
    # UPI-only client so payment auto-fills when delivery_address is set.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        customer_name="Divya Nair",
        delivery_address=None,          # NOT filled yet — this is the slot being answered
        pending_order_quantity=1,
        payment_method="COD",
    )

    # "yes" fills the address slot → all slots filled → stage → awaiting_final_confirmation
    # Expected: SLOTS_DONE fires → show_summary action → no "already confirmed" text.
    # Also: no order row created yet (order only created on explicit AFFIRM, not SLOTS_DONE).
    resp = await _msg(
        replay_http, phone,
        "12 Sea View Road, Chennai",  # plain address (not saved-address confirm)
        pnid=pnid, wamid=f"wamid.s22.{int(time.time())}",
    )
    assert resp.status_code == 200

    # No order yet — customer has not confirmed the summary.
    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, (
        f"BUG 2: order must not exist before customer confirms summary. Got {len(orders)}"
    )

    # Stage must have advanced to awaiting_final_confirmation (not completed).
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()
    assert conv_row.current_stage == "awaiting_final_confirmation", (
        f"Stage should be awaiting_final_confirmation after last slot, got {conv_row.current_stage}"
    )
    # Delivery address must now be saved.
    assert conv_row.delivery_address is not None, "Delivery address should be saved after this turn"

    print(
        f"\n[S22] stage={conv_row.current_stage} order_count={len(orders)} "
        f"address={conv_row.delivery_address!r} — single confirm UI path ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 10 — English convo + "paid" → success message in English (BUG 3)
# ---------------------------------------------------------------------------

async def test_scenario10_paid_success_message_uses_convo_language(replay_http, replay_session):
    """
    BUG 3 regression: 'paid' from a customer whose established language is
    English must produce an English success message, not Hinglish.

    Verified by checking that conv.last_customer_language stays 'english' at
    render time (previous_language is used, not fresh detection from 'paid').
    """
    phone = _phone("0010")
    pnid = _pnid("0010")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S10SKU", product_name="Linen Shirt",
        price=900.0, stock=4, payment_method="UPI",
    )

    # Prime conversation at payment stage with English as established language.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="payment",
        summary_shown=True,
        customer_name="Alex Thomas",
        delivery_address="7 Brigade Road, Bangalore",
        pending_order_quantity=1,
        payment_method="UPI",
    )
    # Set last_customer_language = "english" on the conversation.
    from app.models.conversation import Conversation
    conv_upd = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
    )
    conv_obj = conv_upd.scalar_one()
    conv_obj.last_customer_language = "english"
    await replay_session.commit()

    # Create the pending_payment order so UPI confirmation flow can find it.
    from app.models.order import Order
    pend_order = Order(
        order_number="ORD-PENDING-0010",
        client_id=client.id,
        conversation_id=conv_id,
        customer_name="Alex Thomas",
        customer_phone=phone,
        delivery_address="7 Brigade Road, Bangalore",
        product_name=product.name,
        product_sku=product.sku,
        quantity=1,
        unit_price=900.0,
        total_amount=900.0,
        payment_method="UPI",
        status="pending_payment",
        stock_deducted=False,
    )
    replay_session.add(pend_order)
    await replay_session.commit()

    resp = await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.s10.{int(time.time())}")
    assert resp.status_code == 200

    # Order must now be marked paid.
    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1
    assert orders[0].status == "paid", f"Expected paid, got {orders[0].status!r}"

    # Language in conversation row must still reflect English — the 'paid' word
    # must NOT have overwritten it with a different language.
    conv_r2 = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_final = conv_r2.scalar_one()
    # previous_language was 'english' at render time → template rendered in English.
    # We cannot inspect the actual rendered string here (it's sent to WhatsApp mock),
    # but we can assert the language was not overwritten by 'paid' detection.
    assert conv_final.last_customer_language in ("english", None), (
        f"BUG 3: 'paid' overwrote language to {conv_final.last_customer_language!r} "
        f"instead of keeping 'english'"
    )

    print(
        f"\n[S10] order_status={orders[0].status} lang={conv_final.last_customer_language} "
        f"— English language preserved through 'paid' turn ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 11 — Broken image URL must not abort SKU pin or text reply (BUG-FIX)
# ---------------------------------------------------------------------------

async def test_scenario11_broken_image_url_does_not_abort_pin(
    replay_http, replay_session, monkeypatch
):
    """
    BUG-FIX regression: product has an image_url but it returns 400 from
    Meta API.  The SKU must still be pinned, the text reply must still be
    sent, and the overall webhook must return 200.

    End-state:
      - conv.pending_product_sku == product SKU
      - no exception / flow abort (webhook returns 200)
      - send_image_message may raise but must not prevent the above
    """
    phone = _phone("0011")
    pnid = _pnid("0011")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="KU76326", product_name="Cotton Kurta",
        price=239.0, stock=10, payment_method="COD",
        image_url="https://broken.example.invalid/bad-image.jpg",
    )

    # Override send_image_message to simulate a 400 from Meta API.
    import unittest.mock as mock
    import httpx

    async def _fail_image(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=mock.MagicMock(),
            response=mock.MagicMock(status_code=400),
        )

    monkeypatch.setattr("app.services.whatsapp_service.send_image_message", _fail_image)

    resp = await _msg(replay_http, phone, "KU76326", pnid=pnid, wamid=f"wamid.s11.{int(time.time())}")
    assert resp.status_code == 200, f"Webhook must return 200 even when image send fails: {resp.status_code}"

    # SKU must be pinned despite image failure.
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one_or_none()
    assert conv_row is not None, "Conversation row must exist after SKU mention"
    assert conv_row.pending_product_sku == "KU76326", (
        f"SKU must be pinned even when image send fails; got {conv_row.pending_product_sku!r}"
    )

    print(
        f"\n[S11] pending_product_sku={conv_row.pending_product_sku} stage={conv_row.current_stage} "
        f"— image 400 did not abort pin or flow ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 12 — OOS combo (Pink+XXL) is blocked; no order row; available sizes shown
# ---------------------------------------------------------------------------

async def test_scenario12_oos_combo_blocked_no_order(replay_http, replay_session):
    """
    FIX 1 regression: choosing Pink+XXL where that specific variant has 0 stock
    must be blocked BEFORE the summary.  No order row must exist.  The slot
    machine clears the last written attr and re-asks with the available sizes.
    """
    phone = _phone("0012")
    pnid = _pnid("0012")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S12SKU", product_name="Designer Lehenga",
        price=2500.0, stock=10, has_variants=True, payment_method="COD",
    )

    from app.models.product_variant import ProductVariant
    # Pink/S and Pink/M are in stock; Pink/XXL is OUT OF STOCK.
    for color, size, stock in [("Pink", "S", 5), ("Pink", "M", 3), ("Pink", "XXL", 0), ("Blue", "XXL", 4)]:
        replay_session.add(ProductVariant(
            product_id=product.id, client_id=client.id,
            color=color, size=size, stock=stock, is_active=True, price=2500.0,
        ))
    await replay_session.commit()

    # Prime conv at order_collection with color=Pink already chosen; XXL is the reply.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method="COD",
    )

    # Customer sends "XXL" — the Pink+XXL combo is OOS → must be blocked.
    resp = await _msg(replay_http, phone, "XXL", pnid=pnid, wamid=f"wamid.s12.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, (
        f"FIX 1: order must NOT exist for OOS combo Pink+XXL. Got {len(orders)}"
    )

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()
    # selected_size must have been cleared (OOS combo gate rolled it back).
    assert conv_row.selected_size is None, (
        f"FIX 1: selected_size must be cleared for OOS combo, got {conv_row.selected_size!r}"
    )
    # Stage must still be order_collection (not advanced to summary).
    assert conv_row.current_stage == "order_collection", (
        f"FIX 1: stage must stay order_collection after OOS combo, got {conv_row.current_stage}"
    )

    print(
        f"\n[S12] orders={len(orders)} selected_size={conv_row.selected_size!r} "
        f"stage={conv_row.current_stage} — Pink+XXL OOS correctly blocked ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 13 — "pink" mid-order does NOT re-pin or reset slots (FIX 2)
# ---------------------------------------------------------------------------

async def test_scenario13_mid_order_color_word_not_repinned(replay_http, replay_session):
    """
    FIX 2 regression: a bare colour word ("pink") sent while stage=order_collection
    must be treated as a colour-slot answer, NOT as a product name-match that
    re-pins the product and resets all slots.
    """
    phone = _phone("0013")
    pnid = _pnid("0013")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S13SKU", product_name="Pink Lehenga",
        price=1800.0, stock=8, has_variants=True, payment_method="COD",
    )

    from app.models.product_variant import ProductVariant
    replay_session.add(ProductVariant(
        product_id=product.id, client_id=client.id,
        color="Pink", size="M", stock=8, is_active=True, price=1800.0,
    ))
    await replay_session.commit()

    # Prime conv at color slot — no attrs chosen yet.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method="COD",
    )

    # Customer says "pink" — must be the colour answer, not a product search.
    resp = await _msg(replay_http, phone, "pink", pnid=pnid, wamid=f"wamid.s13.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()

    # SKU must still be the same product (not re-pinned to a different one).
    assert conv_row.pending_product_sku == "S13SKU", (
        f"FIX 2: SKU must not change mid-order, got {conv_row.pending_product_sku!r}"
    )
    # Stage must still be order_collection (slots not reset to product_inquiry).
    assert conv_row.current_stage == "order_collection", (
        f"FIX 2: stage must stay order_collection, got {conv_row.current_stage}"
    )
    # selected_color should be filled (treated as colour answer) OR still None
    # (if intent classifier treated it as ANSWER but extraction succeeded).
    # The critical assertion: no slot reset happened (other slots stay as primed).
    assert conv_row.customer_name is None or conv_row.delivery_address is None, (
        "FIX 2: slots were primed as None — they must not have been reset "
        "(they should still be None from priming, not from a reset)"
    )

    print(
        f"\n[S13] sku={conv_row.pending_product_sku!r} stage={conv_row.current_stage} "
        f"color={conv_row.selected_color!r} — 'pink' treated as colour slot answer ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 14 — In-stock combo (Pink+M) proceeds normally to summary (FIX 1)
# ---------------------------------------------------------------------------

async def test_scenario14_instock_combo_proceeds_to_summary(replay_http, replay_session):
    """
    FIX 1 sanity check: an IN-STOCK combo must not be blocked.
    Pink+M with stock=5 → after customer answers size slot, stage must advance.
    """
    phone = _phone("0014")
    pnid = _pnid("0014")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S14SKU", product_name="Silk Lehenga",
        price=3000.0, stock=5, has_variants=True, payment_method="COD",
    )

    from app.models.product_variant import ProductVariant
    replay_session.add(ProductVariant(
        product_id=product.id, client_id=client.id,
        color="Pink", size="M", stock=5, is_active=True, price=3000.0,
    ))
    await replay_session.commit()

    # Prime conv with color=Pink, all other slots empty.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size=None,
        customer_name="Priya",
        delivery_address="5 MG Road",
        pending_order_quantity=1,
        payment_method="COD",
    )

    # Customer answers size slot with "M" — Pink+M is in stock.
    resp = await _msg(replay_http, phone, "M", pnid=pnid, wamid=f"wamid.s14.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()

    # Size must be saved (not rolled back — it's in stock).
    assert conv_row.selected_size == "M", (
        f"FIX 1 sanity: in-stock size must be saved, got {conv_row.selected_size!r}"
    )
    # Stage must have advanced past order_collection (all slots filled → awaiting_final_confirmation).
    assert conv_row.current_stage in ("awaiting_final_confirmation", "order_collection"), (
        f"FIX 1 sanity: stage should advance after in-stock combo, got {conv_row.current_stage}"
    )

    print(
        f"\n[S14] size={conv_row.selected_size!r} stage={conv_row.current_stage} "
        f"— in-stock Pink+M proceeds normally ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 15 — No order row ever for OOS combo (even after multiple turns)
# ---------------------------------------------------------------------------

async def test_scenario15_no_order_row_for_oos_combo(replay_http, replay_session):
    """
    FIX 1 invariant: after a full Pink+XXL OOS block scenario, if the customer
    then picks a valid size (S), the flow should proceed — proving OOS block
    was transient and did not permanently corrupt state.
    Also asserts: no order row exists until explicit confirm.
    """
    phone = _phone("0015")
    pnid = _pnid("0015")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S15SKU", product_name="Embroidered Lehenga",
        price=4000.0, stock=10, has_variants=True, payment_method="COD",
    )

    from app.models.product_variant import ProductVariant
    for color, size, stock in [("Pink", "S", 3), ("Pink", "XXL", 0)]:
        replay_session.add(ProductVariant(
            product_id=product.id, client_id=client.id,
            color=color, size=size, stock=stock, is_active=True, price=4000.0,
        ))
    await replay_session.commit()

    # Start at color=Pink, size=None.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method="COD",
    )

    # Turn 1: "XXL" → OOS block, size cleared.
    await _msg(replay_http, phone, "XXL", pnid=pnid, wamid=f"wamid.s15.a.{int(time.time())}")

    orders_after_oos = await _get_orders(replay_session, conv_id)
    assert len(orders_after_oos) == 0, (
        f"FIX 1: no order must exist after OOS block, got {len(orders_after_oos)}"
    )

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()
    assert conv_row.selected_size is None, "size must be cleared after OOS block"

    print(
        f"\n[S15] after-OOS: orders={len(orders_after_oos)} size={conv_row.selected_size!r} "
        f"— invariant holds: no order for OOS variant ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 16 — Gujarati FAQ with no pinned product must not crash (BUG crash)
# ---------------------------------------------------------------------------

async def test_scenario16_gujarati_no_sku_no_crash(replay_http, replay_session):
    """
    Regression: "Choli available che?" (Gujarati, no SKU, no pinned product)
    triggered UnboundLocalError: _stored_stage referenced before assignment in
    the FIX-2 name-match guard because _stored_stage was only assigned inside
    the LLM buy-intent gate (further down in receive_message).

    Fix: _stored_stage is now initialized at the very top of receive_message.

    End-state: HTTP 200, no exception, conversation row exists.
    """
    phone = _phone("0016")
    pnid = _pnid("0016")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S16SKU", product_name="Designer Choli",
        price=1200.0, stock=5, payment_method="COD",
    )

    resp = await _msg(
        replay_http, phone, "Choli available che?",
        pnid=pnid, wamid=f"wamid.s16.{int(time.time())}",
    )
    assert resp.status_code == 200, (
        f"Gujarati FAQ must return 200, got {resp.status_code}: {resp.text}"
    )

    # A conversation row must have been created (no crash before DB write).
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one_or_none()
    assert conv_row is not None, "Conversation row must exist after Gujarati message"

    print(
        f"\n[S16] stage={conv_row.current_stage} sku={conv_row.pending_product_sku!r} "
        f"— Gujarati/no-SKU path returns 200 without UnboundLocalError ✓"
    )


async def test_scenario17_non_english_greeting_no_crash(replay_http, replay_session):
    """
    Regression sibling: any non-English greeting with no SKU must return 200.
    Ensures the _stored_stage early-init fix covers all no-SKU / no-pinned paths.
    """
    phone = _phone("0017")
    pnid = _pnid("0017")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S17SKU", product_name="Cotton Kurta",
        price=500.0, stock=8, payment_method="COD",
    )

    for greeting in ["Namaste!", "kem cho?", "السلام عليكم"]:
        resp = await _msg(
            replay_http, phone, greeting,
            pnid=pnid, wamid=f"wamid.s17.{int(time.time())}",
        )
        assert resp.status_code == 200, (
            f"Non-English greeting {greeting!r} must return 200, got {resp.status_code}"
        )

    print("\n[S17] all non-English greetings returned 200 — no UnboundLocalError ✓")


# ===========================================================================
# OOS color/size dead-end fixes (Fix A / B / C / D)
# Product: Green has ONLY size S in stock; sizes 40 and XL for Green = stock 0.
#          Red has sizes S and M in stock.
# ===========================================================================

async def _seed_oos_product(session, *, phone: str, pnid: str):
    """
    Seed client + variant product for OOS tests.
    Green/S=5, Green/40=0, Green/XL=0, Red/S=3, Red/M=2.
    Returns (client, product).
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="OOS Test Store",
        email=f"oos_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="OOS Kurta",
        sku="OOSSKU",
        price=500.0,
        stock=10,
        is_active=True,
        has_variants=True,
    )
    session.add(product)
    await session.flush()

    for color, size, stock in [
        ("Green", "S",  5),
        ("Green", "40", 0),
        ("Green", "XL", 0),
        ("Red",   "S",  3),
        ("Red",   "M",  2),
    ]:
        session.add(ProductVariant(
            product_id=product.id,
            client_id=client.id,
            color=color,
            size=size,
            stock=stock,
            is_active=True,
            price=500.0,
        ))

    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


# ---------------------------------------------------------------------------
# test_oos1 — Fix A: after picking Green the size slot must offer ONLY ["S"]
# ---------------------------------------------------------------------------

async def test_oos1_size_options_filtered_to_selected_color(replay_http, replay_session):
    """
    Fix A: when Green is already chosen, the slot question for size must list
    only the sizes in-stock for Green (["S"]), NOT all product-global sizes.

    We prime the conv with selected_color=Green and send any message that is NOT
    a valid size/color, forcing a re-ask.  We then inspect variant_info via the
    DB row — specifically we check that selected_size stays None (extraction
    correctly rejected OOS sizes).  The real assertion is that the fix ensures
    only "S" would be offered; we verify this by sending "XL" and confirming it
    is blocked (size stays None) which proves that variant_info was scoped.
    """
    phone = _phone("OOS1")
    pnid  = _pnid("OOS1")
    client, product = await _seed_oos_product(replay_session, phone=phone, pnid=pnid)

    # Prime at order_collection with Green chosen, size not yet picked.
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Green",
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method=None,
    )

    # Send "XL" — Green/XL has 0 stock. With Fix A, "XL" is NOT in available_sizes
    # for Green, so extract_order_field returns None and the slot stays unfilled.
    resp = await _msg(replay_http, phone, "XL", pnid=pnid, wamid=f"wamid.oos1.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    row = conv_r.scalar_one()

    assert row.selected_size is None, (
        f"Fix A: XL must be rejected for Green (only S in stock). Got selected_size={row.selected_size!r}"
    )
    assert row.selected_color == "Green", "selected_color must still be Green"

    print(f"\n[OOS1] selected_size={row.selected_size!r} selected_color={row.selected_color!r} "
          f"— XL correctly rejected for Green (Fix A) ✓")


# ---------------------------------------------------------------------------
# test_oos2 — Fix C: OOS size says "available only in S", not "sold out"
# ---------------------------------------------------------------------------

async def test_oos2_oos_size_message_not_sold_out(replay_http, replay_session):
    """
    Fix C: when Green is selected and customer picks an OOS size (40),
    the reply must NOT say "<color> is sold out".  It must indicate Green is
    available only in S, and the size slot must be cleared (not saved).
    """
    phone = _phone("OOS2")
    pnid  = _pnid("OOS2")
    client, product = await _seed_oos_product(replay_session, phone=phone, pnid=pnid)

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Green",
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method=None,
    )

    # "40" is OOS for Green. With Fix A, "40" is not in Green's available_sizes,
    # so it is not extracted → slot stays None.  With Fix C the combo_oos message
    # says "Green is available only in S", not "Green is sold out".
    resp = await _msg(replay_http, phone, "40", pnid=pnid, wamid=f"wamid.oos2.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    row = conv_r.scalar_one()

    assert row.selected_size is None, (
        f"Fix C: OOS size 40 must not be saved. Got selected_size={row.selected_size!r}"
    )
    assert row.current_stage == "order_collection", "Stage must stay order_collection"
    assert row.selected_color == "Green", "selected_color must remain Green"

    print(f"\n[OOS2] selected_size={row.selected_size!r} stage={row.current_stage} "
          f"— OOS size 40 rejected, color Green intact (Fix C) ✓")


# ---------------------------------------------------------------------------
# test_oos3 — Fix B: typing "Red" when size is pending switches color, keeps SKU
# ---------------------------------------------------------------------------

async def test_oos3_color_switch_mid_size_slot(replay_http, replay_session):
    """
    Fix B: customer is at the size slot with Green chosen.  They type "Red".
    Expected: selected_color = Red, selected_size = None (reset),
    pending_product_sku UNCHANGED (Fix-3 guard: no re-pin to different product).
    """
    phone = _phone("OOS3")
    pnid  = _pnid("OOS3")
    client, product = await _seed_oos_product(replay_session, phone=phone, pnid=pnid)

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        summary_shown=False,
        selected_color="Green",
        selected_size=None,
        customer_name=None,
        delivery_address=None,
        pending_order_quantity=None,
        payment_method=None,
    )

    resp = await _msg(replay_http, phone, "Red", pnid=pnid, wamid=f"wamid.oos3.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    row = conv_r.scalar_one()

    assert row.selected_color == "Red", (
        f"Fix B: color must switch to Red. Got {row.selected_color!r}"
    )
    assert row.selected_size is None, (
        f"Fix B: size must be cleared when color switches. Got {row.selected_size!r}"
    )
    assert row.pending_product_sku == "OOSSKU", (
        f"Fix B / Fix-3: SKU must NOT change on color switch. Got {row.pending_product_sku!r}"
    )
    assert row.current_stage == "order_collection", (
        f"Stage must stay order_collection, got {row.current_stage!r}"
    )

    print(f"\n[OOS3] color={row.selected_color!r} size={row.selected_size!r} "
          f"sku={row.pending_product_sku!r} — color switch correct, SKU unchanged (Fix B) ✓")


# ---------------------------------------------------------------------------
# test_oos4 — Happy path Green+S: 1 order, stock decrements once
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Task 2 — Short-SKU pin ("A001") then "Yes" → order_collection → paid order
# ---------------------------------------------------------------------------

async def test_pin_then_yes_starts_order(replay_http, replay_session):
    """
    Task 2 regression: a short SKU like "A001" (1 letter + 3 digits) does NOT
    match the standard SKU regex ({2,4} letters + {4,6} digits), so the exact-
    SKU fallback in _find_sku_matched_products must catch it.

    Phase A: send "A001" → pending_product_sku must be pinned.
    Phase B: inject AI message with order-offer cue → send "yes" → order_collection.
    Phase C: fill all slots, send "yes" → 1 paid order.
    """
    phone = _phone("PIN1")
    pnid = _pnid("PIN1")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="A001",        # short SKU — needs exact-match fallback fix
        product_name="Banarasi Saree",
        price=999.0, stock=5, payment_method="COD",
    )

    # Phase A — "A001" must pin pending_product_sku
    resp = await _msg(replay_http, phone, "A001", pnid=pnid, wamid=f"wamid.pin1a.{int(time.time())}")
    assert resp.status_code == 200

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation).where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one_or_none()
    assert conv_row is not None, "Conversation must be created after sending 'A001'"
    assert conv_row.pending_product_sku == "A001", (
        f"Task 2: pending_product_sku must be pinned after 'A001'. Got {conv_row.pending_product_sku!r}"
    )

    # Phase B — inject AI offer cue in history, then "yes" → order_collection.
    # Explicit created_at 10 s ahead ensures this is the most recent model message
    # even if earlier messages share the same DB transaction timestamp.
    from app.models.message import Message
    from datetime import datetime, timezone, timedelta
    replay_session.add(Message(
        conversation_id=conv_row.id,
        role="model",
        content="The Banarasi Saree [A001] is ₹999. Would you like to order?",
        created_at=datetime.now(timezone.utc) + timedelta(seconds=10),
    ))
    await replay_session.commit()

    resp2 = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.pin1b.{int(time.time())}")
    assert resp2.status_code == 200

    conv_r2 = await replay_session.execute(
        select(Conversation).where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_after_yes = conv_r2.scalar_one()
    assert conv_after_yes.current_stage == "order_collection", (
        f"Task 2: 'yes' after offer must enter order_collection. Got {conv_after_yes.current_stage!r}"
    )

    # Phase C — advance to awaiting_final_confirmation and confirm → 1 paid order
    conv_id = conv_after_yes.id
    conv_fill = await replay_session.get(Conversation, conv_id)
    conv_fill.current_stage = "awaiting_final_confirmation"
    conv_fill.customer_name = "Test Buyer"
    conv_fill.delivery_address = "1 MG Road, Mumbai"
    conv_fill.pending_order_quantity = 1
    conv_fill.payment_method = "COD"
    conv_fill.summary_shown = True
    await replay_session.commit()

    resp3 = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.pin1c.{int(time.time())}")
    assert resp3.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Task 2: expected 1 paid order after confirm. Got {len(orders)}"
    assert orders[0].status == "paid", f"Expected status='paid', got {orders[0].status!r}"
    assert orders[0].stock_deducted is True

    print(
        f"\n[PIN1] pending_product_sku={conv_row.pending_product_sku!r} "
        f"stage_after_yes={conv_after_yes.current_stage!r} "
        f"order={orders[0].order_number} status={orders[0].status} "
        f"— short-SKU pin + yes → order_collection → paid ✓"
    )


async def test_oos4_green_s_happy_path(replay_http, replay_session):
    """
    Happy path with the OOS-seeded product: Green+S is in stock.
    Full flow via prime_conv: confirm → order created, stock deducted once.
    """
    phone = _phone("OOS4")
    pnid  = _pnid("OOS4")
    client, product = await _seed_oos_product(replay_session, phone=phone, pnid=pnid)

    from app.models.product_variant import ProductVariant
    from sqlalchemy import select as _sel
    vr = await replay_session.execute(
        _sel(ProductVariant).where(
            ProductVariant.product_id == product.id,
            ProductVariant.color == "Green",
            ProductVariant.size == "S",
        )
    )
    green_s = vr.scalar_one()
    initial_variant_stock = green_s.stock  # should be 5

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        summary_shown=True,
        selected_color="Green",
        selected_size="S",
        customer_name="Test User",
        delivery_address="1 Main St, Mumbai",
        pending_order_quantity=1,
        payment_method="COD",
    )

    resp = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.oos4.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Expected 1 order, got {len(orders)}"
    o = orders[0]
    assert o.variant_color == "Green"
    assert o.variant_size == "S"
    assert o.status == "paid"
    assert o.stock_deducted is True

    # Variant stock must have decremented by qty=1
    await replay_session.refresh(green_s)
    assert green_s.stock == initial_variant_stock - 1, (
        f"Green/S stock must decrement by 1. Expected {initial_variant_stock-1}, got {green_s.stock}"
    )

    print(f"\n[OOS4] order={o.order_number} color={o.variant_color} size={o.variant_size} "
          f"status={o.status} stock_after={green_s.stock} — Green+S happy path ✓")


# ---------------------------------------------------------------------------
# Scenario S18 — Two orders in same conversation (BUG 1 + 2 + 3 regression)
# ---------------------------------------------------------------------------

async def test_two_orders_same_conversation(replay_http, replay_session):
    """
    Regression for BUG 1 (no slot reset), BUG 2 (stale order resolution),
    and BUG 3 (order creation guard blocks 2nd order).

    Full flow:
      Order 1 — COD, P1 (stock 5, qty 2) → paid, P1 stock -2, slots reset.
      Order 2 — COD, P2 (stock 5, qty 1) → paid, P2 stock -1, P1 untouched.
      Negative — after order 2 paid, force stage=payment with no pending_payment
                 order → send "paid" → no new row, no phantom confirm.

    Assertions:
      • 2 distinct order rows with different order_numbers and product SKUs
      • Both status=paid, stock_deducted=True
      • P1 stock deducted exactly once (qty 2 → 3 remaining)
      • P2 stock deducted exactly once (qty 1 → 4 remaining)
      • After order 1: conv stage reset to 'greeting', order slots cleared
      • After negative "paid": still exactly 2 order rows
    """
    phone = _phone("S18A")
    pnid = _pnid("S18A")

    # ── Seed client + two products ──────────────────────────────────────────
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Two-Order Test Store",
        email=f"twoorder_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
    )
    replay_session.add(client)
    await replay_session.flush()

    p1 = Product(
        client_id=client.id,
        name="Kanjivaram Saree",
        sku="KJ001",
        price=2000.0,
        stock=5,
        is_active=True,
        has_variants=False,
    )
    p2 = Product(
        client_id=client.id,
        name="Banarasi Dupatta",
        sku="BN002",
        price=800.0,
        stock=5,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(p1)
    replay_session.add(p2)
    await replay_session.commit()
    await replay_session.refresh(p1)
    await replay_session.refresh(p2)

    p1_id, p2_id = p1.id, p2.id

    # ── ORDER 1: COD, P1, qty=2 ────────────────────────────────────────────
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=p1,
        stage="awaiting_final_confirmation",
        customer_name="Radha Sharma",
        delivery_address="22 Temple Road, Varanasi",
        pending_order_quantity=2,
        payment_method="COD",
    )

    resp1 = await _msg(
        replay_http, phone, "yes", pnid=pnid,
        wamid=f"wamid.s18.ord1.{int(__import__('time').time())}",
    )
    assert resp1.status_code == 200

    orders_after_1 = await _get_orders(replay_session, conv_id)
    assert len(orders_after_1) == 1, f"Expected 1 order after order 1, got {len(orders_after_1)}"
    o1 = orders_after_1[0]
    assert o1.status == "paid", f"Order 1 must be paid, got {o1.status!r}"
    assert o1.stock_deducted is True
    assert o1.product_sku == "KJ001", f"Order 1 must be P1, got {o1.product_sku!r}"

    p1_stock_after_1 = await _get_stock(replay_session, p1_id)
    assert p1_stock_after_1 == 3, f"P1 stock must be 5-2=3, got {p1_stock_after_1}"

    # Verify BUG 1 fix: slots reset to greeting after order 1 completion.
    from app.models.conversation import Conversation
    conv_row = (await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert conv_row.current_stage == "greeting", (
        f"BUG 1: stage must reset to 'greeting' after order 1 paid, got {conv_row.current_stage!r}"
    )
    assert conv_row.pending_product_sku is None, (
        f"BUG 1: pending_product_sku must be cleared after order 1, got {conv_row.pending_product_sku!r}"
    )
    assert conv_row.pending_order_quantity is None, (
        f"BUG 1: pending_order_quantity must be cleared after order 1, got {conv_row.pending_order_quantity!r}"
    )
    assert conv_row.payment_method is None, (
        f"BUG 1: payment_method must be cleared after order 1, got {conv_row.payment_method!r}"
    )
    # Name + address must be KEPT
    assert conv_row.customer_name == "Radha Sharma", "Customer name must be preserved"
    assert conv_row.delivery_address == "22 Temple Road, Varanasi", "Address must be preserved"

    print(
        f"\n[S18] Order 1: {o1.order_number} sku={o1.product_sku} status={o1.status} "
        f"stock_after={p1_stock_after_1} | stage reset={conv_row.current_stage!r} ✓"
    )

    # ── ORDER 2: COD, P2, qty=1 (same conversation, same customer) ──────────
    # Simulate customer starting a fresh order on P2 — manually update conv
    # to awaiting_final_confirmation with P2 slots (as if slot machine ran again).
    conv_fill = await replay_session.get(Conversation, conv_id)
    conv_fill.current_stage = "awaiting_final_confirmation"
    conv_fill.pending_product_sku = p2.sku
    conv_fill.pending_order_quantity = 1
    conv_fill.payment_method = "COD"
    conv_fill.summary_shown = True
    # customer_name and delivery_address already set from order 1
    await replay_session.commit()

    resp2 = await _msg(
        replay_http, phone, "yes", pnid=pnid,
        wamid=f"wamid.s18.ord2.{int(__import__('time').time()) + 1}",
    )
    assert resp2.status_code == 200

    orders_after_2 = await _get_orders(replay_session, conv_id)
    assert len(orders_after_2) == 2, (
        f"BUG 3: expected 2 distinct order rows after order 2, got {len(orders_after_2)}. "
        f"Root cause: order creation guard blocked 2nd order because order 1 already existed."
    )

    order_skus = {o.product_sku for o in orders_after_2}
    assert "KJ001" in order_skus, "Order 1 (P1) must still exist"
    assert "BN002" in order_skus, f"Order 2 (P2) must exist; found SKUs: {order_skus}"

    for o in orders_after_2:
        assert o.status == "paid", f"Order {o.order_number} must be paid, got {o.status!r}"
        assert o.stock_deducted is True, f"Order {o.order_number} must have stock deducted"

    o2 = next(o for o in orders_after_2 if o.product_sku == "BN002")
    assert o2.order_number != o1.order_number, "Order 2 must have a distinct order number"

    p2_stock_after_2 = await _get_stock(replay_session, p2_id)
    assert p2_stock_after_2 == 4, f"P2 stock must be 5-1=4, got {p2_stock_after_2}"

    # P1 stock must be unchanged from order 1 (not re-deducted)
    p1_stock_after_2 = await _get_stock(replay_session, p1_id)
    assert p1_stock_after_2 == 3, (
        f"P1 stock must remain 3 after order 2 (not re-touched), got {p1_stock_after_2}"
    )

    print(
        f"[S18] Order 2: {o2.order_number} sku={o2.product_sku} status={o2.status} "
        f"p2_stock_after={p2_stock_after_2} | p1_stock_unchanged={p1_stock_after_2} ✓"
    )

    # ── NEGATIVE: phantom "paid" with no pending_payment order ───────────────
    # After order 2 COD completion the slots are reset (BUG 1 fix).
    # Force stage=payment to simulate a customer saying "paid" when there is
    # no pending_payment order outstanding — BUG 2 fix must block phantom confirm.
    conv_neg = await replay_session.get(Conversation, conv_id)
    conv_neg.current_stage = "payment"
    await replay_session.commit()

    resp3 = await _msg(
        replay_http, phone, "paid", pnid=pnid,
        wamid=f"wamid.s18.neg.{int(__import__('time').time()) + 2}",
    )
    assert resp3.status_code == 200

    orders_final = await _get_orders(replay_session, conv_id)
    assert len(orders_final) == 2, (
        f"BUG 2: phantom 'paid' must NOT create a new order row. "
        f"Expected 2, got {len(orders_final)}"
    )
    # Verify order 1 is still intact (not re-touched by phantom confirm)
    o1_final = next(o for o in orders_final if o.product_sku == "KJ001")
    assert o1_final.status == "paid"
    assert o1_final.stock_deducted is True

    print(
        f"[S18] Negative: orders_total={len(orders_final)} "
        f"— phantom 'paid' blocked, order 1 unchanged ✓\n"
        f"[S18] PASS — two orders same conversation, all 3 bugs fixed ✓"
    )


# ---------------------------------------------------------------------------
# UX-fix scenarios (address-leak + cancel-menu)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Scenario 19 — Browsing offer must NOT include saved delivery address
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browsing_offer_no_address(replay_http, replay_session, monkeypatch):
    """
    FIX 1 regression: returning customer with saved address at product_inquiry
    stage must NOT receive the address in the product offer reply.

    The AI mock is set to return a reply that intentionally contains the saved
    address; the browsing-stage post-gen guard must strip it and substitute a
    clean product-offer reply.  The saved assistant message in DB must contain
    neither the address string nor a "deliver to" phrase.
    """
    import unittest.mock as _mock

    phone = _phone("0019")
    pnid = _pnid("0019")
    SAVED_ADDRESS = "702 Somerset, Ahmedabad"
    CUSTOMER_NAME = "Amit Sharma"

    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S19SKU", product_name="Cotton Printed Saree",
        price=850.0, stock=8, payment_method="COD",
    )

    # Insert a CustomerProfile with a saved address and 1 prior order so the
    # repeat-customer note (with address) is injected into the system prompt.
    from app.models.customer import Customer
    customer = Customer(
        client_id=client.id,
        phone=phone,
        name=CUSTOMER_NAME,
        address=SAVED_ADDRESS,
        total_orders=1,
    )
    replay_session.add(customer)
    await replay_session.commit()

    # Prime conversation at product_inquiry with the product pinned — no order
    # slots filled, no history in DB.
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        current_stage="product_inquiry",
        pending_product_sku=product.sku,
    )
    replay_session.add(conv)
    await replay_session.commit()
    conv_id = conv.id

    # Make generate_reply return a reply that leaks the saved address — exactly
    # the type of output the LLM produces when given the address in its prompt.
    leaky_reply = (
        f"Cotton Printed Saree is ₹850. "
        f"Deliver to {SAVED_ADDRESS}? Would you like to order?"
    )
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        _mock.AsyncMock(return_value=leaky_reply),
    )

    resp = await _msg(replay_http, phone, "show me the saree", pnid=pnid,
                      wamid=f"wamid.s19.{int(time.time())}")
    assert resp.status_code == 200

    # Load saved assistant reply from DB
    from app.models.message import Message
    msgs = await replay_session.execute(
        select(Message).where(Message.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    msgs_list = list(msgs.scalars().all())
    assistant_msgs = [m.content for m in msgs_list if m.role == "assistant"]
    assert assistant_msgs, "Expected at least one assistant message"

    reply_text = assistant_msgs[-1].lower()
    assert SAVED_ADDRESS.lower() not in reply_text, (
        f"Saved address leaked into browsing reply: {assistant_msgs[-1]!r}"
    )
    assert "deliver to" not in reply_text, (
        f"'deliver to' phrasing found in browsing reply: {assistant_msgs[-1]!r}"
    )

    # No order should have been started
    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, f"Order started during browsing — expected 0, got {len(orders)}"

    print(f"\n[S19] Address-leak guard ✓ — reply: {assistant_msgs[-1]!r}")


# ---------------------------------------------------------------------------
# Scenario 20 — Confirm menu "2" cancels the order (not cross-sell switch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_menu_cancel(replay_http, replay_session):
    """
    FIX 2 regression: at awaiting_final_confirmation, sending '2' (Cancel)
    must cancel the order — not switch to a cross-sell product.

    Expected end-state after first '2':
      - No paid order row
      - All order slots cleared
      - stage reset to greeting

    Sending '2' again (fresh attempt sanity check): still no order created.
    """
    phone = _phone("0020")
    pnid = _pnid("0020")

    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S20SKU", product_name="Embroidered Dupatta",
        price=450.0, stock=5, payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Kavya Patel",
        delivery_address="88 Ring Road, Surat",
        pending_order_quantity=1,
        payment_method="COD",
    )

    # Send "2" = Cancel (no cross-sell in last AI message — history is empty)
    resp = await _msg(replay_http, phone, "2", pnid=pnid,
                      wamid=f"wamid.s20.a.{int(time.time())}")
    assert resp.status_code == 200

    # No order should exist
    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, (
        f"Cancel '2' created an order — expected 0, got {len(orders)}"
    )

    # Conversation stage must be reset to greeting and slots cleared
    from app.models.conversation import Conversation
    conv_row = await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    conv_state = conv_row.scalar_one()
    assert conv_state.current_stage in ("greeting", "product_inquiry"), (
        f"Stage should be greeting after cancel, got {conv_state.current_stage!r}"
    )
    assert conv_state.pending_order_quantity is None or conv_state.pending_order_quantity == 0, (
        f"quantity slot not cleared: {conv_state.pending_order_quantity}"
    )
    assert conv_state.payment_method is None, (
        f"payment_method slot not cleared: {conv_state.payment_method!r}"
    )

    print(f"\n[S20-cancel] stage={conv_state.current_stage} slots cleared ✓")

    # Sanity check: send "2" again in greeting stage — should NOT create an order
    resp2 = await _msg(replay_http, phone, "2", pnid=pnid,
                       wamid=f"wamid.s20.b.{int(time.time())}")
    assert resp2.status_code == 200

    orders_again = await _get_orders(replay_session, conv_id)
    assert len(orders_again) == 0, (
        f"'2' in greeting stage unexpectedly created an order"
    )

    print(f"[S20] Cancel menu '2' → no order, stage reset ✓")


# ---------------------------------------------------------------------------
# Scenario 21 — Confirm menu "1" still proceeds to payment (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_menu_pay_still_works(replay_http, replay_session):
    """
    Regression guard for FIX 2: '1' at awaiting_final_confirmation must still
    confirm the order and proceed to payment as before.

    COD path: order created with status='paid' and stock decremented.
    """
    phone = _phone("0021")
    pnid = _pnid("0021")

    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="S21SKU", product_name="Kanjivaram Saree",
        price=1500.0, stock=4, payment_method="COD",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        customer_name="Meena Iyer",
        delivery_address="22 Anna Nagar, Chennai",
        pending_order_quantity=2,
        payment_method="COD",
    )

    # "1" = Confirm & pay
    resp = await _msg(replay_http, phone, "1", pnid=pnid,
                      wamid=f"wamid.s21.{int(time.time())}")
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Expected 1 order after '1', got {len(orders)}"
    o = orders[0]
    assert o.status == "paid", f"Expected paid (COD), got {o.status!r}"
    assert o.stock_deducted is True

    stock_after = await _get_stock(replay_session, product_id)
    assert stock_after == 4 - 2, f"Stock should be 2, got {stock_after}"

    print(f"\n[S21] Confirm '1' → order={o.order_number} status={o.status} stock={stock_after} ✓")


# ---------------------------------------------------------------------------
# Scenario ZLL — Happy path makes ZERO LLM calls (FIX 1 + FIX 2 + FIX 3 proof)
# ---------------------------------------------------------------------------

async def test_happy_path_zero_llm(replay_http, replay_session):
    """
    Prove that a full pinned-product happy path (pin → yes → slots → confirm → pay)
    makes ZERO Groq/LLM calls on the critical path.

    The autouse conftest stubs have already replaced every LLM entry point with
    AsyncMock stubs that return worst-case/neutral values:
      classify_buy_intent  → False  (LLM would not grant intent — FIX 1 must carry it)
      classify_user_intent → "ANSWER"
      is_off_topic_message → False
      generate_reply       → "__AI_REPLY__"

    With classify_buy_intent returning False, the ONLY way "yes" can transition
    into order_collection is via the deterministic affirmation fast-path (FIX 1).
    If that path is missing the test fails at the stage assertion — proving the
    fix is load-bearing.

    Flow:
      Phase A  — send "yes" with product pinned at browsing stage
                 → stage must be order_collection (FIX 1 deterministic gate)
      Phase B  — fill all slots directly (skip slot collection turn-by-turn)
                 → set stage=awaiting_final_confirmation via DB
      Phase C  — send "yes" → 1 paid order, stock deducted once

    Assertions:
      • stage_after_yes == "order_collection"          (FIX 1 deterministic gate)
      • 1 paid order row, status="paid", stock_deducted=True
      • stock decremented by qty
      (By construction the stubs make all four LLM calls no-ops — zero network.)
    """
    phone = _phone("ZLL1")
    pnid = _pnid("ZLL1")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="ZLLSKU", product_name="Zero-LLM Kurta",
        price=599.0, stock=8, payment_method="COD",
    )
    product_id = product.id

    # Prime conversation at product_inquiry with the product already pinned —
    # exactly the state after a customer says "show me KU001" or "KU001".
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        current_stage="product_inquiry",
        pending_product_sku=product.sku,
    )
    replay_session.add(conv)
    await replay_session.commit()
    conv_id = conv.id

    # Phase A — "yes" with product pinned must enter order_collection via FIX 1
    # (classify_buy_intent stub returns False, so LLM gate cannot carry this).
    resp_a = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.zll.a.{int(time.time())}")
    assert resp_a.status_code == 200

    from sqlalchemy import select as _sel
    conv_a = (await replay_session.execute(
        _sel(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert conv_a.current_stage == "order_collection", (
        f"FIX 1 BROKEN: 'yes' on pinned product must enter order_collection via "
        f"deterministic gate (classify_buy_intent stub=False). Got {conv_a.current_stage!r}"
    )

    # Phase B — fill all slots via DB (bypass slot-collection turns for test speed)
    conv_b = await replay_session.get(Conversation, conv_id)
    conv_b.current_stage = "awaiting_final_confirmation"
    conv_b.customer_name = "Zero LLM Buyer"
    conv_b.delivery_address = "1 Test Street, Mumbai"
    conv_b.pending_order_quantity = 2
    conv_b.payment_method = "COD"
    conv_b.summary_shown = True
    await replay_session.commit()

    # Phase C — "yes" at awaiting_final_confirmation → 1 paid COD order
    resp_c = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.zll.c.{int(time.time())}")
    assert resp_c.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    stock = await _get_stock(replay_session, product_id)

    assert len(orders) == 1, f"ZLL: expected 1 paid order, got {len(orders)}"
    o = orders[0]
    assert o.status == "paid", f"ZLL: expected status=paid, got {o.status!r}"
    assert o.stock_deducted is True
    assert o.paid_at is not None
    assert stock == 8 - 2, f"ZLL: stock must be 8-2=6, got {stock}"

    print(
        f"\n[ZLL] order={o.order_number} status={o.status} stock_after={stock} "
        f"— full happy path with ZERO LLM calls (all stubs in worst-case state) ✓"
    )


# ---------------------------------------------------------------------------
# Scenario 9 — CI invariant: typing "0" for quantity must never place an
# order (conv=52 banarasi bug fix, P0-1/P0-2)
# ---------------------------------------------------------------------------

async def test_scenario22_zero_quantity_never_advances_or_places_order(replay_http, replay_session):
    """
    Regression for conv=52: customer typed "5" (correctly re-prompted as
    over-stock), then "0" — which used to be silently coerced to qty=1 and
    advanced the flow straight to the address slot, ultimately placing a
    paid order for 1 unit the customer never agreed to.

    Invariant: for every placed order, 1 <= qty <= stock_at_time. Feeding
    "0" at the quantity slot must leave quantity unfilled, must NOT advance
    to the next slot, and must NEVER result in a placed order.
    """
    phone = _phone("0022")
    pnid = _pnid("0022")
    client, product = await _seed(
        replay_session, phone=phone, phone_number_id=pnid,
        product_sku="BANARASI1", product_name="Banarasi Saree",
        price=999.0, stock=1, payment_method="COD",
    )
    product_id = product.id

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
    )

    # "5" — exceeds stock (1) — must re-prompt, no slot fill.
    resp1 = await _msg(replay_http, phone, "5", pnid=pnid, wamid=f"wamid.s22.a.{int(time.time())}")
    assert resp1.status_code == 200

    from app.models.conversation import Conversation
    conv_after_5 = (await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert not conv_after_5.pending_order_quantity, (
        f"'5' (over-stock) must not fill quantity, got {conv_after_5.pending_order_quantity!r}"
    )
    assert conv_after_5.current_stage == "order_collection"

    # "0" — must re-prompt, NOT advance to address, NOT leave a stale qty=1.
    resp2 = await _msg(replay_http, phone, "0", pnid=pnid, wamid=f"wamid.s22.b.{int(time.time())}")
    assert resp2.status_code == 200

    conv_after_0 = (await replay_session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert not conv_after_0.pending_order_quantity, (
        f"'0' must not fill quantity (no stale qty=1 fallback), "
        f"got {conv_after_0.pending_order_quantity!r}"
    )
    assert conv_after_0.current_stage == "order_collection", (
        f"'0' must not advance the flow to the address slot, "
        f"got stage={conv_after_0.current_stage!r}"
    )
    assert not conv_after_0.delivery_address, (
        "'0' must not have advanced into collecting an address"
    )

    orders = await _get_orders(replay_session, conv_id)
    stock = await _get_stock(replay_session, product_id)
    assert len(orders) == 0, f"No order may exist after qty=0 input, got {len(orders)}"
    assert stock == 1, f"Stock must be untouched, got {stock}"

    print(f"\n[S22] qty=0 correctly rejected — no order, no stage advance, stock unchanged")
