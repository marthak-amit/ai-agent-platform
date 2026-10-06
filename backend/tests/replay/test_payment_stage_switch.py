"""
Replay tests for mid-payment product-switch confirmation.

Previously, an explicit new-purchase-intent message ("I want to buy X",
"switch to X") sent while stage="payment" was not classified — it fell
through to the generic fallback, which just re-sent the same payment
reminder and silently ignored the customer's new request.

Fix: is_purchase_intent() + a confident catalogue match now trigger an
explicit switch-confirmation prompt (current_stage="awaiting_switch_confirm",
candidate stashed in interrupted_sku) instead of either silently overwriting
the pending order or silently ignoring it. run_switch_confirm_guard resolves
"yes"/"no" on the next turn.

Scenarios:
  1. Purchase-intent message during payment -> explicit switch-confirm
     prompt, order NOT silently overwritten.
  2. "yes" -> pending_product_sku switched, fresh payment prompt with the
     new product's amount.
  3. "no" -> original order/amount preserved, original reminder re-sent.

All tests run against a real Postgres instance (replay_ci_test) and are
skipped automatically when local Postgres is not reachable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import send_button, send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


def _phone(suffix: str) -> str:
    return f"91940000{suffix}"


def _pnid(suffix: str) -> str:
    return f"555{suffix}"


async def _seed(session: AsyncSession, *, phone: str, pnid: str, **kwargs):
    return await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=pnid, **kwargs
    )


async def _add_second_product(session: AsyncSession, *, client_id: int, sku: str, name: str, price: float, stock: int = 10):
    from app.models.product import Product
    product = Product(
        client_id=client_id, name=name, sku=sku, price=price,
        stock=stock, is_active=True, has_variants=False,
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product


async def _prime_conv(session: AsyncSession, *, phone: str, product, stage: str, **fields):
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage=stage,
        **fields,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def _get_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


async def _msg(http_client, phone: str, text: str, *, pnid: str):
    return await send_message(http_client, phone, text, phone_number_id=pnid)


async def _btn(http_client, phone: str, btn_id: str, btn_title: str, *, pnid: str, wamid: str | None = None):
    return await send_button(http_client, phone, btn_id, btn_title, wamid=wamid, phone_number_id=pnid)


async def _get_orders(session: AsyncSession, conv_id: int):
    from app.models.order import Order
    r = await session.execute(
        select(Order).where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


def _last_reply(whatsapp_service_mock) -> str:
    call_args, call_kwargs = whatsapp_service_mock._raw_send_text_message.call_args_list[-1]
    return call_kwargs.get("message_text") or (call_args[1] if len(call_args) > 1 else "")


# ===========================================================================
# 1. Purchase-intent mid-payment -> explicit switch-confirm prompt
# ===========================================================================

async def test_purchase_intent_midpayment_prompts_switch_confirm(replay_http, replay_session):
    """
    "I want to buy kurti dress" while stage=payment (order pending for a
    saree) must ask an explicit switch-confirm question — NOT silently
    overwrite pending_product_sku, and NOT just repeat the payment reminder.
    """
    phone = _phone("0001")
    pnid = _pnid("0001")
    client, saree = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="SAREE01", product_name="Cotton Printed Saree",
        price=850.0, stock=5, payment_method="UPI",
    )
    kurti = await _add_second_product(
        replay_session, client_id=client.id, sku="KURTI01",
        name="Printed Kurti Dress", price=650.0,
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=saree,
        stage="payment",
        pending_product_sku=saree.sku,
        pending_order_quantity=1,
        customer_name="Test Customer",
        delivery_address="123 Test Street",
        payment_method="UPI",
        flow_state_at=recent,
    )

    from app.services import whatsapp_service
    whatsapp_service._raw_send_text_message.reset_mock()

    resp = await _msg(replay_http, phone, "I want to buy kurti dress", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = _last_reply(whatsapp_service)
    assert "cotton printed saree" in reply.lower(), f"Expected current product named, got: {reply!r}"
    assert "printed kurti dress" in reply.lower(), f"Expected candidate product named, got: {reply!r}"
    assert "yes" in reply.lower() and "no" in reply.lower()

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_switch_confirm"
    assert conv.interrupted_sku == kurti.sku
    assert conv.pending_product_sku == saree.sku, "Order must not be silently overwritten"


# ===========================================================================
# 2./3. Confirm "yes" / "no" with a REAL pending order (manual-UPI flow)
# ===========================================================================
#
# In the manual-UPI flow an Order row (pending_payment, stock reserved, QR bound to its amount and
# number) exists the moment the customer is in the payment stage. These scenarios therefore start by
# really confirming an order, then switching — the old ones primed a payment-stage conversation with
# NO order, which hid that "yes" re-sent the OLD order's instruction (old amount, old QR).

async def _order_in_payment(replay_session, replay_http, monkeypatch, *, suffix: str, saree_sku: str, kurti_sku: str,
                            kurti_stock: int = 10):
    """Confirm an order for a saree (₹850), then ask to buy a kurti (₹650): returns the ids + captured sends."""
    import unittest.mock as mock

    from tests.replay.helpers import stub_media_and_capture_sends

    sent = stub_media_and_capture_sends(monkeypatch)
    buttons = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", buttons)

    phone, pnid = _phone(suffix), _pnid(suffix)
    client, saree = await _seed(
        replay_session, phone=phone, pnid=pnid, product_sku=saree_sku, product_name="Cotton Printed Saree",
        price=850.0, stock=5, payment_method="UPI",
    )
    kurti = await _add_second_product(
        replay_session, client_id=client.id, sku=kurti_sku, name="Printed Kurti Dress", price=650.0,
        stock=kurti_stock,
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=saree, stage="awaiting_final_confirmation",
        pending_product_sku=saree.sku, pending_order_quantity=1, customer_name="Test Customer",
        delivery_address="123 Test Street", mobile_number=phone, payment_method="UPI", summary_shown=True,
        flow_state_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    resp = await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay", pnid=pnid, wamid=f"wamid.sw{suffix}.confirm")
    assert resp.status_code == 200, resp.text
    first_orders = await _get_orders(replay_session, conv_id)
    assert len(first_orders) == 1 and first_orders[0].status == "pending_payment" and first_orders[0].total_amount == 850.0

    resp = await _msg(replay_http, phone, "I want to buy kurti dress", pnid=pnid)
    assert resp.status_code == 200, resp.text
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_switch_confirm" and conv.interrupted_sku == kurti.sku
    sent["texts"].clear()
    sent["images"].clear()
    buttons.reset_mock()
    return dict(phone=phone, pnid=pnid, client=client, saree=saree, kurti=kurti, conv_id=conv_id,
                old_order=first_orders[0], sent=sent, buttons=buttons)


async def test_switch_confirm_yes_switches_order(replay_http, replay_session, monkeypatch):
    """
    "yes" to the switch prompt: the old unpaid order is cancelled (stock released), the conversation moves to
    the CONFIRM step for the new product (new amount, old variant slots dropped), and confirming creates a
    brand-new order — own number, amount and QR — so the customer never pays for the old one.
    """
    from app.models.order import Order

    ctx = await _order_in_payment(replay_session, replay_http, monkeypatch, suffix="0002",
                                  saree_sku="SAREE02", kurti_sku="KURTI02")
    phone, pnid, conv_id, kurti = ctx["phone"], ctx["pnid"], ctx["conv_id"], ctx["kurti"]

    resp = await _msg(replay_http, phone, "yes", pnid=pnid)
    assert resp.status_code == 200, resp.text

    old = (await replay_session.execute(
        select(Order).where(Order.id == ctx["old_order"].id).execution_options(populate_existing=True))).scalar_one()
    assert old.status == "cancelled" and old.cancel_reason == "cancelled by customer"
    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == kurti.sku, "Order must be switched to the candidate"
    assert conv.interrupted_sku is None
    assert conv.current_stage == "awaiting_final_confirmation" and conv.summary_shown is True

    summary = ctx["buttons"].call_args.kwargs["body_text"]
    assert "Printed Kurti Dress" in summary and "650" in summary, summary
    assert "850" not in summary and "Cotton Printed Saree" not in summary, f"Must not show the old order: {summary!r}"
    assert [b["title"] for b in ctx["buttons"].call_args.kwargs["buttons"]] == ["Confirm & Pay", "Cancel"]

    # Confirming creates the NEW order with its own instruction (number, new amount, UPI id, QR + closing line).
    ctx["sent"]["texts"].clear()
    resp = await _btn(replay_http, phone, "confirm_pay", "Confirm & Pay", pnid=pnid, wamid="wamid.sw0002.confirm2")
    assert resp.status_code == 200, resp.text
    orders = await _get_orders(replay_session, conv_id)
    assert [o.status for o in sorted(orders, key=lambda o: o.id)] == ["cancelled", "pending_payment"]
    new = max(orders, key=lambda o: o.id)
    assert new.total_amount == 650.0 and new.product_sku == kurti.sku and new.order_number != old.order_number
    text = "\n".join(ctx["sent"]["texts"])
    assert new.order_number in text and "650" in text and "test@upi" in text.lower() and "850" not in text
    assert ctx["sent"]["images"] and "send the payment screenshot" in ctx["sent"]["images"][-1][1]
    assert (await _get_conv(replay_session, conv_id)).current_stage == "payment"


async def test_switch_confirm_no_keeps_original_order(replay_http, replay_session, monkeypatch):
    """
    "no" discards the switch candidate and re-sends the SAME open order's instruction (its own number and
    ₹850 amount) — the order stays pending_payment and nothing about the candidate leaks into the reply.
    """
    from app.models.order import Order

    ctx = await _order_in_payment(replay_session, replay_http, monkeypatch, suffix="0003",
                                  saree_sku="SAREE03", kurti_sku="KURTI03")
    phone, pnid, conv_id, saree = ctx["phone"], ctx["pnid"], ctx["conv_id"], ctx["saree"]

    resp = await _msg(replay_http, phone, "no", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = "\n".join(ctx["sent"]["texts"])
    assert ctx["old_order"].order_number in reply, f"Expected the original order's instruction, got: {reply!r}"
    assert "850" in reply and "650" not in reply
    assert "test@upi" in reply.lower() and "send the payment screenshot" in reply

    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == saree.sku, "Original order must be preserved"
    assert conv.interrupted_sku is None and conv.current_stage == "payment"
    old = (await replay_session.execute(
        select(Order).where(Order.id == ctx["old_order"].id).execution_options(populate_existing=True))).scalar_one()
    assert old.status == "pending_payment"


async def test_switch_confirm_yes_is_declined_while_payment_proof_is_under_review(replay_http, replay_session, monkeypatch):
    """A screenshot is already with the seller (payment_submitted): the order can't be swapped away, and the customer is told why."""
    from app.models.order import Order

    ctx = await _order_in_payment(replay_session, replay_http, monkeypatch, suffix="0005",
                                  saree_sku="SAREE05", kurti_sku="KURTI05")
    phone, pnid, conv_id, saree = ctx["phone"], ctx["pnid"], ctx["conv_id"], ctx["saree"]
    old = (await replay_session.execute(select(Order).where(Order.id == ctx["old_order"].id))).scalar_one()
    old.status = "payment_submitted"
    await replay_session.commit()

    resp = await _msg(replay_http, phone, "yes", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = "\n".join(ctx["sent"]["texts"])
    assert "already with our team for verification" in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == saree.sku and conv.interrupted_sku is None and conv.current_stage == "payment"
    fresh = (await replay_session.execute(
        select(Order).where(Order.id == old.id).execution_options(populate_existing=True))).scalar_one()
    assert fresh.status == "payment_submitted"


async def test_switch_confirm_yes_to_a_sold_out_product_keeps_the_order(replay_http, replay_session, monkeypatch):
    """The candidate sold out while the customer was deciding: say so, keep the original unpaid order untouched."""
    from app.models.order import Order

    ctx = await _order_in_payment(replay_session, replay_http, monkeypatch, suffix="0006",
                                  saree_sku="SAREE06", kurti_sku="KURTI06")
    phone, pnid, conv_id, kurti = ctx["phone"], ctx["pnid"], ctx["conv_id"], ctx["kurti"]
    kurti.stock = 0
    await replay_session.commit()

    resp = await _msg(replay_http, phone, "yes", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = "\n".join(ctx["sent"]["texts"])
    assert "Printed Kurti Dress" in reply and ctx["old_order"].order_number in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == ctx["saree"].sku and conv.current_stage == "payment"
    old = (await replay_session.execute(
        select(Order).where(Order.id == ctx["old_order"].id).execution_options(populate_existing=True))).scalar_one()
    assert old.status == "pending_payment"


# ===========================================================================
# 4. Free-text cancel-intent during payment -> order cancelled (BUG 2)
# ===========================================================================
#
# Previously there was no cancel-intent classifier at payment stage: only a
# literal "cancel" button/keyword was recognised (run_cancel_in_payment_guard).
# Free text like "I do not want to buy this" fell through to the generic
# fallback, which just re-sent the payment reminder and left the order stuck.

async def test_free_text_cancel_intent_during_payment_cancels_order(replay_http, replay_session):
    """
    "I do not want to buy this" while stage=payment (pending UPI order) must
    cancel the order and clear all order slots — not just re-show the
    payment reminder.
    """
    phone = _phone("0004")
    pnid = _pnid("0004")
    client, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="SAREE04", product_name="Cotton Printed Saree",
        price=850.0, stock=5, payment_method="UPI",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        pending_product_sku=product.sku,
        pending_order_quantity=1,
        customer_name="Test Customer",
        delivery_address="123 Test Street",
        payment_method="UPI",
        summary_shown=True,
    )

    # Confirm -> pending_payment order created, stage -> payment
    resp = await _btn(
        replay_http, phone, "confirm_pay", "Confirm & Pay", pnid=pnid,
        wamid=f"wamid.sw0004.confirm.{id(conv_id)}",
    )
    assert resp.status_code == 200, resp.text

    orders_before = await _get_orders(replay_session, conv_id)
    assert orders_before and orders_before[0].status == "pending_payment"

    from app.services import whatsapp_service
    whatsapp_service._raw_send_text_message.reset_mock()

    resp = await _msg(replay_http, phone, "I do not want to buy this", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = _last_reply(whatsapp_service)
    assert "cancel" in reply.lower(), f"Expected a cancellation reply, got: {reply!r}"

    orders_after = await _get_orders(replay_session, conv_id)
    assert orders_after and orders_after[0].status == "cancelled", (
        f"Expected order cancelled, got status={orders_after[0].status if orders_after else None!r}"
    )

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting"
    assert conv.pending_product_sku is None
    assert conv.pending_order_quantity is None
    assert conv.customer_name is None
    assert conv.delivery_address is None


# ===========================================================================
# 5. Cancel-intent during awaiting_switch_confirm -> whole order cancelled,
#    not just the switch declined (BUG 2)
# ===========================================================================

async def test_cancel_intent_during_switch_confirm_cancels_whole_order(replay_http, replay_session):
    """
    "cancel" sent in reply to the mid-payment switch-confirm prompt must
    cancel the ENTIRE order (both the original pending item and the
    candidate switch), not be read as an unclear/negative answer to the
    switch question (which would just keep the original order running).
    """
    phone = _phone("0005")
    pnid = _pnid("0005")
    client, saree = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="SAREE05", product_name="Cotton Printed Saree",
        price=850.0, stock=5, payment_method="UPI",
    )
    kurti = await _add_second_product(
        replay_session, client_id=client.id, sku="KURTI05",
        name="Printed Kurti Dress", price=650.0,
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=saree,
        stage="awaiting_final_confirmation",
        pending_product_sku=saree.sku,
        pending_order_quantity=1,
        customer_name="Test Customer",
        delivery_address="123 Test Street",
        payment_method="UPI",
        summary_shown=True,
    )

    resp = await _btn(
        replay_http, phone, "confirm_pay", "Confirm & Pay", pnid=pnid,
        wamid=f"wamid.sw0005.confirm.{id(conv_id)}",
    )
    assert resp.status_code == 200, resp.text

    # Purchase-intent message -> switch-confirm prompt opens
    resp = await _msg(replay_http, phone, "I want to buy kurti dress", pnid=pnid)
    assert resp.status_code == 200, resp.text
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_switch_confirm"
    assert conv.interrupted_sku == kurti.sku

    from app.services import whatsapp_service
    whatsapp_service._raw_send_text_message.reset_mock()

    # Cancel, not yes/no
    resp = await _msg(replay_http, phone, "cancel", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = _last_reply(whatsapp_service)
    assert "cancel" in reply.lower(), f"Expected a cancellation reply, got: {reply!r}"

    orders_after = await _get_orders(replay_session, conv_id)
    assert orders_after and orders_after[0].status == "cancelled", (
        f"Expected the original order cancelled, got status="
        f"{orders_after[0].status if orders_after else None!r}"
    )

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting"
    assert conv.pending_product_sku is None, "Whole order must be cancelled, not just the switch"
    assert conv.interrupted_sku is None


# ===========================================================================
# 6. Fuzzy-matched switch candidate renders a clean name (BUG 1 regression)
# ===========================================================================
#
# The switch-confirm prompt interpolates the candidate straight from the
# canonical Product.name column resolved by _find_confident_product_match —
# there is no separate fuzzy-search/match-label field in play. This test
# locks that in for a genuinely fuzzy match (the query only overlaps the
# candidate via its DESCRIPTION, not an exact full-name match) so a future
# change can't silently start rendering a score/label suffix.

async def test_switch_confirm_candidate_name_renders_clean(replay_http, replay_session):
    """
    A switch candidate resolved via a fuzzy (description-assisted) match
    must render with its exact, clean product name in the switch-confirm
    prompt — no "(best match)", score, or other suffix.
    """
    phone = _phone("0006")
    pnid = _pnid("0006")
    client, saree = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="SAREE06", product_name="Cotton Printed Saree",
        price=850.0, stock=5, payment_method="UPI",
    )
    # Candidate matched partly via its description, not just its bare name —
    # a genuinely "fuzzy" (score>=3 from name+description) match.
    from app.models.product import Product
    kurti = Product(
        client_id=client.id, name="Kurti", sku="KURTI06", price=650.0,
        stock=10, is_active=True, has_variants=False,
        description="Cotton printed kurti, block-print design.",
    )
    replay_session.add(kurti)
    await replay_session.commit()
    await replay_session.refresh(kurti)

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=saree,
        stage="payment",
        pending_product_sku=saree.sku,
        pending_order_quantity=1,
        customer_name="Test Customer",
        delivery_address="123 Test Street",
        payment_method="UPI",
    )

    from app.services import whatsapp_service
    whatsapp_service._raw_send_text_message.reset_mock()

    resp = await _msg(replay_http, phone, "switch to cotton printed kurti instead", pnid=pnid)
    assert resp.status_code == 200, resp.text

    reply = _last_reply(whatsapp_service)
    assert "switch to kurti instead" in reply.lower(), (
        f"Expected the clean candidate name 'Kurti' with no suffix, got: {reply!r}"
    )

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_switch_confirm"
    assert conv.interrupted_sku == kurti.sku
