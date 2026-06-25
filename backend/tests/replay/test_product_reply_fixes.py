"""
Replay tests for FIX 0–4:
  FIX 0 — price formatting everywhere (no ₹2,450.0 float renders)
  FIX 1 — deterministic compact product reply when SKU is pinned (no LLM)
  FIX 2 — order summary has UPI notice + delivery estimate
  FIX 3 — order confirmation has catalogue link + success emoji
  FIX 4 — interactive buttons confirm_pay / cancel_order; typed fallback

All tests use a real Postgres instance (replay_ci_test).
Skipped automatically when local Postgres is not reachable.
"""

from __future__ import annotations

import re
import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import (
    _PG_AVAILABLE,
    WA_PHONE_NUMBER_ID,
    seed_client_and_product,
    seed_variant_and_simple,
)
from tests.replay.helpers import capture_all, send_button, send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _phone(suffix: str) -> str:
    return f"91800000{suffix}"


def _pnid(suffix: str) -> str:
    return f"999{suffix}"


async def _seed(session: AsyncSession, *, phone: str, pnid: str, **kwargs):
    """Insert client + product; returns (client, product)."""
    return await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=pnid, **kwargs
    )


async def _prime_conv(session: AsyncSession, *, phone: str, product, stage: str, **slots):
    """Insert a conversation pre-loaded with slot values; return conv_id."""
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


def _all_sent_text(mock_fn) -> str:
    """Concatenate all message_text args passed to send_text_message mock."""
    parts = []
    for call in mock_fn.call_args_list:
        args, kwargs = call
        text = kwargs.get("message_text") or (args[1] if len(args) > 1 else "")
        parts.append(str(text))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# FIX 1 — deterministic compact product reply when SKU is pinned (no LLM)
# ---------------------------------------------------------------------------

async def test_compact_product_reply(replay_http, replay_session, monkeypatch):
    """
    When pending_product_sku is set at product_inquiry stage, the reply must be
    deterministic and compact:
      <name> [<SKU>] — ₹<int>
      Available colors: ...
      Available sizes: ...
      Would you like to order?
    No product description. No float price. No LLM call.
    """
    phone = _phone("0100")
    pnid = _pnid("100")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX1SKU",
        product_name="Cotton Kurta",
        price=2450.0,
        stock=10,
        has_variants=True,
        payment_method="COD",
    )

    from app.models.product_variant import ProductVariant
    for color, size in [("Red", "M"), ("Blue", "M"), ("Red", "XL")]:
        replay_session.add(ProductVariant(
            product_id=product.id,
            client_id=client.id,
            color=color,
            size=size,
            stock=5,
            is_active=True,
            price=2450.0,
        ))
    await replay_session.commit()

    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=client.id,
        current_stage="product_inquiry",
        pending_product_sku=product.sku,
        summary_shown=False,
    )
    replay_session.add(conv)
    await replay_session.commit()

    # Track LLM call count before the request
    from app.services import gemini_service
    ai_calls_before = gemini_service.generate_reply.call_count

    captured_list = capture_all(monkeypatch)

    resp = await send_message(replay_http, phone, "tell me about this kurta", phone_number_id=pnid)
    assert resp.status_code == 200

    assert gemini_service.generate_reply.call_count == ai_calls_before, (
        "generate_reply was called — FIX 1 deterministic path did not fire"
    )

    captured = "\n".join(captured_list)

    assert "Cotton Kurta" in captured, "Product name missing from compact reply"
    assert "FIX1SKU" in captured, "SKU missing from compact reply"
    assert "₹2,450" in captured, "Formatted price ₹2,450 missing"
    assert "₹2,450.0" not in captured, "Float price leaked — format_price not applied"
    assert "Would you like to order" in captured, "Order CTA missing"
    # Variant info must appear
    assert "Available colors" in captured or "Available sizes" in captured, (
        "Variant info missing from compact reply"
    )


# ---------------------------------------------------------------------------
# FIX 0 + FIX 2 — UPI notice + delivery estimate + formatted price in summary
# ---------------------------------------------------------------------------

async def test_summary_has_delivery_and_upi(replay_http, replay_session):
    """
    Order summary must include:
    - 'UPI' mention (payment notice line)
    - '3–7 business days' delivery estimate
    - Formatted price ₹2,450 (no decimal digits)
    """
    phone = _phone("0200")
    pnid = _pnid("200")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX2SKU",
        product_name="Silk Saree",
        price=2450.0,
        stock=5,
        payment_method="UPI",
    )

    # Prime at order_collection with all slots filled so any message triggers
    # SLOTS_DONE → show_summary (which includes UPI notice + delivery estimate).
    await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="order_collection",
        customer_name="Priya Sharma",
        delivery_address="45 MG Road, Bangalore",
        pending_order_quantity=1,
        payment_method="UPI",
        summary_shown=False,
    )

    from app.services import whatsapp_service
    whatsapp_service.send_text_message.reset_mock()
    whatsapp_service.send_button_message.reset_mock()

    # Any neutral message with all slots already filled → SLOTS_DONE → show_summary
    resp = await send_message(replay_http, phone, "ok", phone_number_id=pnid)
    assert resp.status_code == 200

    # Summary is sent via confirm_buttons (send_button_message) — capture body text
    text_captured = _all_sent_text(whatsapp_service.send_text_message)
    btn_body = ""
    for call in whatsapp_service.send_button_message.call_args_list:
        _, kw = call
        btn_body += kw.get("body_text", "") + "\n"
    captured = text_captured + "\n" + btn_body

    assert "UPI" in captured, "UPI payment notice missing from summary"
    assert "business days" in captured, "Delivery estimate missing from summary"
    assert re.search(r"₹2,450(?!\.\d)", captured), "Formatted price ₹2,450 missing"
    assert "₹2,450.0" not in captured, "Float price leaked in summary"


# ---------------------------------------------------------------------------
# FIX 3 — order confirmation has catalogue link and success emoji
# ---------------------------------------------------------------------------

async def test_confirmation_has_catalogue_link(replay_http, replay_session):
    """
    After a COD order is confirmed, the reply must contain:
    - 🎉 success emoji
    - The client's catalogue URL built from catalogue_slug
    - 'placed successfully' text
    """
    phone = _phone("0300")
    pnid = _pnid("300")

    from app.models.client import Client
    from app.models.product import Product

    client_obj = Client(
        business_name="Catalogue Test Store",
        email=f"cattest_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
        upi_id=None,
        catalogue_slug="catalogue-test-store",
    )
    replay_session.add(client_obj)
    await replay_session.flush()

    product = Product(
        client_id=client_obj.id,
        name="Embroidered Lehenga",
        sku="FIX3SKU",
        price=3500.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(product)
    await replay_session.commit()
    await replay_session.refresh(client_obj)
    await replay_session.refresh(product)

    await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Meera Joshi",
        delivery_address="12 Park Street, Mumbai",
        pending_order_quantity=1,
        payment_method="COD",
        summary_shown=False,
    )

    from app.services import whatsapp_service
    whatsapp_service.send_text_message.reset_mock()

    resp = await send_message(replay_http, phone, "yes", phone_number_id=pnid)
    assert resp.status_code == 200

    captured = _all_sent_text(whatsapp_service.send_text_message)

    assert "🎉" in captured, "Success emoji missing from confirmation"
    assert "placed successfully" in captured, "Placed-successfully text missing"
    assert "catalogue-test-store" in captured, "Catalogue slug/URL missing from confirmation"


# ---------------------------------------------------------------------------
# FIX 4 — interactive button confirm_pay → order created and confirmed
# ---------------------------------------------------------------------------

async def test_button_confirm(replay_http, replay_session):
    """
    Tapping confirm_pay button at awaiting_final_confirmation must behave
    identically to typing '1' — an order row must be created and paid.
    """
    phone = _phone("0400")
    pnid = _pnid("400")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX4ASKU",
        product_name="Chanderi Suit",
        price=1800.0,
        stock=8,
        payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Anita Singh",
        delivery_address="33 Civil Lines, Jaipur",
        pending_order_quantity=1,
        payment_method="COD",
        summary_shown=False,
    )

    resp = await send_button(
        replay_http, phone, "confirm_pay", "Confirm & Pay", phone_number_id=pnid
    )
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Expected 1 order after confirm_pay button, got {len(orders)}"
    assert orders[0].status == "paid", f"Expected paid, got {orders[0].status!r}"


# ---------------------------------------------------------------------------
# FIX 4 — interactive button cancel_order → no order created
# ---------------------------------------------------------------------------

async def test_button_cancel(replay_http, replay_session):
    """
    Tapping cancel_order button at awaiting_final_confirmation must cancel the
    flow — no order row should be created.
    """
    phone = _phone("0500")
    pnid = _pnid("500")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX4BSKU",
        product_name="Banarasi Dupatta",
        price=950.0,
        stock=15,
        payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Sunita Rao",
        delivery_address="7 Lake View, Hyderabad",
        pending_order_quantity=1,
        payment_method="COD",
        summary_shown=False,
    )

    resp = await send_button(
        replay_http, phone, "cancel_order", "Cancel", phone_number_id=pnid
    )
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, f"Expected 0 orders after cancel_order button, got {len(orders)}"


# ---------------------------------------------------------------------------
# FIX 4 — typed 'cancel' fallback still works
# ---------------------------------------------------------------------------

async def test_typed_cancel_fallback(replay_http, replay_session):
    """
    Typing 'cancel' at awaiting_final_confirmation must also cancel the flow
    (backward-compat fallback alongside the button).
    """
    phone = _phone("0600")
    pnid = _pnid("600")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX4CSKU",
        product_name="Ikat Saree",
        price=750.0,
        stock=6,
        payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Kavita Mehta",
        delivery_address="20 Gandhi Nagar, Ahmedabad",
        pending_order_quantity=1,
        payment_method="COD",
        summary_shown=False,
    )

    resp = await send_message(replay_http, phone, "cancel", phone_number_id=pnid)
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, f"Expected 0 orders after typed 'cancel', got {len(orders)}"


# ---------------------------------------------------------------------------
# FIX 0 — no decimal prices in any reply type
# ---------------------------------------------------------------------------

async def test_price_formatting_no_decimals(replay_http, replay_session):
    """
    Assert that no reply ever emits a ₹ price with decimal digits (e.g. ₹2598.0).
    Exercises the summary → confirmation flow so both templates are covered.
    """
    phone = _phone("0700")
    pnid = _pnid("700")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="FIX0SKU",
        product_name="Georgette Kurti",
        price=1299.0,
        stock=20,
        payment_method="COD",
    )

    await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Ritu Agarwal",
        delivery_address="5 DLF Colony, Delhi",
        pending_order_quantity=2,
        payment_method="COD",
        summary_shown=False,
    )

    from app.services import whatsapp_service
    whatsapp_service.send_text_message.reset_mock()

    # Trigger summary display ("1" at awaiting_final_confirmation)
    resp1 = await send_message(replay_http, phone, "1", phone_number_id=pnid)
    assert resp1.status_code == 200

    # Trigger confirmation ("yes" after summary)
    resp2 = await send_message(replay_http, phone, "yes", phone_number_id=pnid)
    assert resp2.status_code == 200

    captured = _all_sent_text(whatsapp_service.send_text_message)
    decimal_match = re.search(r"₹[\d,]+\.\d", captured)
    assert decimal_match is None, (
        f"Decimal price found: {decimal_match.group()!r}\n"
        f"Full captured output:\n{captured}"
    )


# ---------------------------------------------------------------------------
# NEW — test_summary_buttons: order summary sent as interactive button message
# ---------------------------------------------------------------------------

async def test_summary_buttons(replay_http, replay_session):
    """
    At awaiting_final_confirmation the order summary must be dispatched via
    send_button_message (confirm_pay + cancel_order) not plain text.
    Tapping confirm_pay must create an order; tapping cancel_order must not.
    """
    phone = _phone("0800")
    pnid = _pnid("800")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="SB_SKU",
        product_name="Chanderi Suit",
        price=1500.0,
        stock=5,
        payment_method="COD",
    )

    await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Pooja Verma",
        delivery_address="10 Nehru Road, Pune",
        pending_order_quantity=1,
        payment_method="COD",
        summary_shown=False,
    )

    from app.services import whatsapp_service
    whatsapp_service.send_button_message.reset_mock()

    # Any message at AFC triggers show_summary → should send via button
    resp = await send_message(replay_http, phone, "show me the order", phone_number_id=pnid)
    assert resp.status_code == 200

    # send_button_message must have been called with confirm_pay + cancel_order
    btn_calls = whatsapp_service.send_button_message.call_args_list
    assert len(btn_calls) >= 1, "send_button_message never called for order summary"
    call_kwargs = btn_calls[-1][1] if btn_calls[-1][1] else {}
    call_args = btn_calls[-1][0]
    buttons = call_kwargs.get("buttons") or (call_args[2] if len(call_args) > 2 else [])
    btn_ids = {b["id"] for b in buttons}
    # Button IDs are nonce-encoded as "{action}~{conv_id}~{nonce}"; match by prefix.
    assert any(bid.split("~")[0] == "confirm_pay" for bid in btn_ids), (
        f"confirm_pay missing from buttons: {btn_ids}"
    )
    assert any(bid.split("~")[0] == "cancel_order" for bid in btn_ids), (
        f"cancel_order missing from buttons: {btn_ids}"
    )


async def test_summary_buttons_confirm_pay_creates_order(replay_http, replay_session):
    """confirm_pay button at AFC must create a paid order."""
    phone = _phone("0810")
    pnid = _pnid("810")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="SB_CONF_SKU",
        product_name="Ikat Dupatta",
        price=600.0,
        stock=10,
        payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Leela Das",
        delivery_address="5 Civil Lines, Bhopal",
        pending_order_quantity=1,
        payment_method="COD",
    )

    resp = await send_button(replay_http, phone, "confirm_pay", "Confirm & Pay", phone_number_id=pnid)
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 1, f"Expected 1 order after confirm_pay, got {len(orders)}"
    assert orders[0].status == "paid"


async def test_summary_buttons_cancel_order_no_order(replay_http, replay_session):
    """cancel_order button at AFC must not create any order."""
    phone = _phone("0820")
    pnid = _pnid("820")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="SB_CAN_SKU",
        product_name="Bandhani Saree",
        price=900.0,
        stock=4,
        payment_method="COD",
    )

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product,
        stage="awaiting_final_confirmation",
        customer_name="Hema Kulkarni",
        delivery_address="22 Laxmi Nagar, Nagpur",
        pending_order_quantity=1,
        payment_method="COD",
    )

    resp = await send_button(replay_http, phone, "cancel_order", "Cancel", phone_number_id=pnid)
    assert resp.status_code == 200

    orders = await _get_orders(replay_session, conv_id)
    assert len(orders) == 0, f"Expected 0 orders after cancel_order, got {len(orders)}"


# ---------------------------------------------------------------------------
# NEW — test_availability_shows_variants: deterministic reply for generic query
# ---------------------------------------------------------------------------

async def test_availability_shows_variants(replay_http, replay_session, monkeypatch):
    """
    Generic availability query ("is it available?") with pending_product_sku set
    must return deterministic compact reply with price + colors + sizes — no LLM.
    """
    phone = _phone("0900")
    pnid = _pnid("900")

    client, product = await _seed(
        replay_session,
        phone=phone,
        pnid=pnid,
        product_sku="AV_SKU",
        product_name="Georgette Party Wear",
        price=3200.0,
        stock=15,
        has_variants=True,
        payment_method="UPI",
    )

    from app.models.product_variant import ProductVariant
    for color, size, stock in [("Red", "M", 3), ("Red", "XL", 2), ("Blue", "M", 5)]:
        replay_session.add(ProductVariant(
            product_id=product.id,
            client_id=client.id,
            color=color,
            size=size,
            stock=stock,
            is_active=True,
            price=3200.0,
        ))
    await replay_session.commit()

    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=client.id,
        current_stage="product_inquiry",
        pending_product_sku=product.sku,
        summary_shown=False,
    )
    replay_session.add(conv)
    await replay_session.commit()

    from app.services import gemini_service
    ai_calls_before = gemini_service.generate_reply.call_count

    captured_list = capture_all(monkeypatch)

    # Generic availability query with no product-name keywords → score=0 in old code
    resp = await send_message(replay_http, phone, "is it available?", phone_number_id=pnid)
    assert resp.status_code == 200

    assert gemini_service.generate_reply.call_count == ai_calls_before, (
        "generate_reply was called — _pinned_relevant gate was not removed"
    )

    captured = "\n".join(captured_list)
    assert "Georgette Party Wear" in captured, "Product name missing"
    assert "AV_SKU" in captured, "SKU missing"
    assert "₹3,200" in captured, "Formatted price missing"
    assert "₹3,200.0" not in captured, "Float price leaked"
    assert "Available colors" in captured, "Colors line missing"
    assert "Available sizes" in captured, "Sizes line missing"
    assert "Would you like to order" in captured, "Order CTA missing"
    # Must NOT contain product description
    assert "description" not in captured.lower(), "Description leaked into compact reply"


# ---------------------------------------------------------------------------
# NEW — test_color_scoped_sizes: OOS sizes excluded after color selection
# ---------------------------------------------------------------------------

async def test_color_scoped_sizes(replay_http, replay_session):
    """
    After the customer selects a color, only in-stock sizes for that color
    must be offered. A size that is OOS for the chosen color must not appear.

    Seed: Pink+M=5, Pink+XXL=0, Blue+M=5, Blue+XXL=3
    When color=Pink → available sizes must be ['M'] (not ['M','XXL']).
    When color=Blue → available sizes must be ['M','XXL'].
    """
    phone = _phone("1000")
    pnid = _pnid("1000")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    # Verify directly via get_in_stock_options — no HTTP needed for this check
    from app.services import catalogue_service

    pink_opts = await catalogue_service.get_in_stock_options(
        replay_session, var_prod.id, color="Pink"
    )
    blue_opts = await catalogue_service.get_in_stock_options(
        replay_session, var_prod.id, color="Blue"
    )

    assert pink_opts["sizes"] == ["M"], (
        f"Pink sizes should be ['M'] (XXL stock=0), got {pink_opts['sizes']}"
    )
    assert set(blue_opts["sizes"]) == {"M", "XXL"}, (
        f"Blue sizes should be {{M, XXL}} (both in-stock), got {blue_opts['sizes']}"
    )
    assert "XXL" not in pink_opts["sizes"], "XXL must be excluded for Pink (stock=0)"
