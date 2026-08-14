"""
Replay tests for FIX 1–5 (abuse protection, aside questions, address validation).

All tests hit a real Postgres (replay_ci_test).  Skipped automatically when
Postgres is not reachable.

Scenarios:
  1. Address slot: junk inputs never saved; valid address accepted.
  2. Payment stage: cross-product price question answered, order unchanged.
  3. Order_collection: delivery-charge question answered, slot re-asked.
  4. Change-address intent: prefix stripped + validated, or ask for new.
  5. Cross-product question mid-order: other product price answered, order kept
     unless the customer explicitly confirms the switch-offer (yes/no).
  6. Ack at slot ("I got it"): forward-moving prompt, not full OOS paragraph.
  7. Happy-path end-to-end: order completes despite new validators.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


def _phone(suffix: str) -> str:
    return f"919800{suffix}"


def _pnid(suffix: str) -> str:
    return f"222{suffix}"


async def _seed(session: AsyncSession, *, phone: str, pnid: str, **kwargs):
    from tests.replay.conftest import seed_client_and_product
    return await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=pnid, **kwargs
    )


async def _prime_conv(session: AsyncSession, *, phone: str, product, stage: str, **slots):
    """Insert a conversation with pre-filled slots."""
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


async def _get_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


async def _get_last_assistant_msg(session: AsyncSession, conv_id: int) -> str:
    from app.models.message import Message
    r = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv_id, Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(1)
    )
    m = r.scalar_one_or_none()
    return (m.content or "") if m else ""


async def _msg(http, phone: str, text: str, *, pnid: str):
    return await send_message(http, phone, text, phone_number_id=pnid)


async def _seed_saved_address_gate(session: AsyncSession, *, conv_id: int, address: str):
    """
    Make the next turn land on the address-confirmation gate: a Customer row
    with a saved address, plus a prior assistant message offering it.
    """
    from app.models.customer import Customer
    from app.models.message import Message
    from app.models.conversation import Conversation

    conv = (await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
    )).scalar_one()

    customer = Customer(client_id=conv.client_id, phone=conv.phone_number, address=address)
    session.add(customer)
    session.add(Message(
        conversation_id=conv_id, role="model",
        content=f"Deliver to: {address}? (yes/change)",
    ))
    await session.commit()


# ---------------------------------------------------------------------------
# Scenario 1 — Address slot: junk inputs rejected, valid address accepted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s1_address_validation(replay_http, replay_session):
    """
    FIX 1: Junk messages must never be saved as delivery_address.
    Valid address must be accepted.
    """
    phone = _phone("010001")
    pnid = _pnid("010001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="ADR001", product_name="Kanjivaram Saree", price=8400.0,
        stock=5, payment_method="COD",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=1,
        customer_name="Priya",
        # delivery_address intentionally left None
    )

    # 1a — question must not be saved as address
    await _msg(replay_http, phone, "how much delivery charge will be?", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is None, "question must not be saved as address"

    # 1b — bare "yes" must not be saved
    await _msg(replay_http, phone, "yes", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is None, "'yes' must not be saved as address"

    # 1c — short text must not be saved
    await _msg(replay_http, phone, "ok", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is None, "stop-word must not be saved as address"

    # 1d — change-it-to verbatim should NOT be saved (just the prefixed question)
    await _msg(replay_http, phone, "change it to s-11 new road, goa", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    # The change-address handler should extract "s-11 new road, goa" — valid address
    assert conv.delivery_address is not None, "valid address from change-intent should be saved"
    assert "s-11" in conv.delivery_address.lower() or "new road" in conv.delivery_address.lower()

    # 1e — previous valid address preserved after a subsequent junk attempt
    prev_addr = conv.delivery_address
    await _msg(replay_http, phone, "?", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address == prev_addr, "valid address must not be overwritten by junk"


# ---------------------------------------------------------------------------
# Scenario 2 — Payment stage: price question answered, order unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s2_payment_stage_price_question(replay_http, replay_session):
    """
    FIX 2/4: During payment, a price question is answered in one line
    and the reply appends the current order context. Stage stays 'payment'.
    """
    phone = _phone("020001")
    pnid = _pnid("020001")
    client, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="PAY001", product_name="Banarasi Saree", price=8400.0,
        stock=5, payment_method="UPI",
    )
    # Seed a second product for the cross-product question
    from app.models.product import Product as PModel
    other = PModel(
        client_id=client.id,
        name="Chanderi Dupatta",
        sku="PAY002",
        price=2200.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(other)
    await replay_session.commit()

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="payment",
        pending_order_quantity=1,
        customer_name="Meera",
        delivery_address="12 Rose Garden, Mumbai 400001",
        payment_method="UPI",
    )

    await _msg(replay_http, phone, "price of Chanderi Dupatta?", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "payment", "stage must not change on a question"
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    # Answer should mention the other product's price
    assert "2,200" in reply or "2200" in reply or "chanderi" in reply.lower(), (
        f"answer should mention Chanderi Dupatta price, got: {reply!r}"
    )
    # Must NOT switch the active order
    assert conv.pending_product_sku == "PAY001", "active order SKU must not change"


# ---------------------------------------------------------------------------
# Scenario 3 — Order_collection: delivery-charge question answered, slot re-asked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s3_delivery_charge_question_mid_order(replay_http, replay_session):
    """
    FIX 2: During order_collection, asking about delivery charges gets a
    one-line answer + slot re-ask. No slot changes.
    """
    phone = _phone("030001")
    pnid = _pnid("030001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="DC001", product_name="Silk Lehenga", price=5000.0,
        stock=8, payment_method="COD",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=1,
        customer_name="Ananya",
        # delivery_address not set yet
    )

    await _msg(replay_http, phone, "how much delivery charges?", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is None, "delivery_address must not be set by question"
    assert conv.current_stage == "order_collection", "stage must not change"
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    assert reply, "must produce a reply"
    # Should mention delivery info (free or time) AND re-ask the slot
    lower = reply.lower()
    assert (
        "deliver" in lower or "free" in lower or "address" in lower
    ), f"reply should address the delivery question or re-ask slot, got: {reply!r}"


# ---------------------------------------------------------------------------
# Scenario 4 — Change-address intent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s4_change_address_intent(replay_http, replay_session):
    """
    FIX 3: 'change it to <address>' extracts and saves the valid address.
           'I just want to change the address' prompts for new address.
    """
    phone = _phone("040001")
    pnid = _pnid("040001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="CA001", product_name="Georgette Saree", price=3200.0,
        stock=5, payment_method="COD",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=1,
        customer_name="Divya",
    )

    # 4a — change-it-to with valid address: must save the extracted portion
    await _msg(replay_http, phone, "change it to s-11 new road, goa 403001", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is not None, "valid address from change-intent should be saved"
    assert "s-11" in (conv.delivery_address or "").lower() or "new road" in (conv.delivery_address or "").lower()

    # 4b — pure intent without address: bot asks for address
    # First reset address to re-test pure intent
    from app.services.conversation_service import update_order_field
    from sqlalchemy.ext.asyncio import AsyncSession as AS2
    await update_order_field(replay_session, conv_id, "delivery_address", None)
    conv.delivery_address = None
    await replay_session.commit()

    await _msg(replay_http, phone, "I just want to change the address", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is None, "pure intent must not save any address"
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    assert "address" in reply.lower(), f"bot should ask for new address, got: {reply!r}"


# ---------------------------------------------------------------------------
# Scenario 5 — Cross-product question mid-order: answered, active order kept
# ---------------------------------------------------------------------------

async def _seed_cross_product_scenario(replay_session, *, suffix: str):
    phone = _phone(suffix)
    pnid = _pnid(suffix)
    client, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="CP001", product_name="Kanjivaram Saree", price=8400.0,
        stock=5, payment_method="COD",
    )
    from app.models.product import Product as PModel
    other = PModel(
        client_id=client.id,
        name="Banarasi Saree",
        sku="CP002",
        price=6500.0,
        stock=3,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(other)
    await replay_session.commit()

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=2,
        customer_name="Rekha",
        # delivery_address pending
    )
    return phone, pnid, conv_id


@pytest.mark.asyncio
async def test_s5_cross_product_question_mid_order(replay_http, replay_session):
    """
    FIX 4: Asking price of a different product mid-order answers the price
    in one line and offers to switch the order to it, instead of re-asking
    the original pinned slot. The active order is NOT switched until the
    customer confirms (see test_s5b/test_s5c below).
    """
    phone, pnid, conv_id = await _seed_cross_product_scenario(replay_session, suffix="050001")

    await _msg(replay_http, phone, "give me price of Banarasi Saree?", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "CP001", "active SKU must not switch on a mere question"
    assert conv.current_stage == "awaiting_order_switch_confirm", (
        "stage must move to the switch-confirm micro-stage, not stay in order_collection"
    )
    assert conv.interrupted_sku == "CP002", "named product SKU must be stashed for confirmation"
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    assert "6,500" in reply or "6500" in reply or "banarasi" in reply.lower(), (
        f"reply should mention Banarasi Saree price, got: {reply!r}"
    )
    assert "yes" in reply.lower() and "no" in reply.lower(), (
        f"reply should offer to switch the order (Yes/No), got: {reply!r}"
    )


@pytest.mark.asyncio
async def test_s5b_cross_product_switch_confirmed(replay_http, replay_session):
    """
    Replying 'yes' to the switch-confirm prompt pins the newly-named
    product, resets variant/qty slots, and starts slot-filling for it.
    """
    phone, pnid, conv_id = await _seed_cross_product_scenario(replay_session, suffix="050002")
    await _msg(replay_http, phone, "give me price of Banarasi Saree?", pnid=pnid)

    await _msg(replay_http, phone, "yes", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "CP002", "confirming 'yes' must switch the pinned SKU"
    assert conv.current_stage == "order_collection", "must return to order_collection to fill slots"
    assert conv.interrupted_sku is None, "interrupted_sku must be cleared after resolution"
    assert not conv.pending_order_quantity, "quantity slot must be reset for the new product"
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    assert "banarasi" in reply.lower(), f"reply should confirm/ask about Banarasi Saree, got: {reply!r}"


@pytest.mark.asyncio
async def test_s5c_cross_product_switch_declined(replay_http, replay_session):
    """
    Replying 'no' to the switch-confirm prompt keeps the original pinned
    product/slots untouched and re-asks the slot that was pending before
    the aside-question interrupted it.
    """
    phone, pnid, conv_id = await _seed_cross_product_scenario(replay_session, suffix="050003")
    await _msg(replay_http, phone, "give me price of Banarasi Saree?", pnid=pnid)

    await _msg(replay_http, phone, "no", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "CP001", "declining must keep the original pinned SKU"
    assert conv.current_stage == "order_collection", "must return to order_collection"
    assert conv.interrupted_sku is None, "interrupted_sku must be cleared after resolution"
    assert conv.pending_order_quantity == 2, "original slots must be untouched"


# ---------------------------------------------------------------------------
# Scenario 6 — Ack at colour slot: forward-moving prompt, not full OOS text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s6_ack_at_slot_forward_moving(replay_http, replay_session):
    """
    FIX 5: 'I got it' / 'achha got it' at a slot should produce a short
    forward-moving prompt, not the full off-topic/OOS paragraph.
    """
    phone = _phone("060001")
    pnid = _pnid("060001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="ACK001", product_name="Silk Dupatta", price=1200.0,
        stock=5, payment_method="COD", has_variants=False,
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        # quantity not set yet
    )

    await _msg(replay_http, phone, "I got it", pnid=pnid)

    conv = await _get_conv(replay_session, conv_id)
    reply = await _get_last_assistant_msg(replay_session, conv_id)
    # Should be short and forward-moving
    assert len(reply) < 300, f"ack reply should be short, got {len(reply)} chars: {reply!r}"
    assert "no problem" in reply.lower() or "piece" in reply.lower() or "quantity" in reply.lower(), (
        f"ack reply should be forward-moving, got: {reply!r}"
    )
    # Must not contain the full OOS paragraph
    assert "out of stock" not in reply.lower() or len(reply) < 150, (
        f"ack reply must not repeat the full OOS text: {reply!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 7 — Happy-path end-to-end still works (regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s7_happy_path_order_completes(replay_http, replay_session):
    """
    FIX 1–5 must not break the COD happy path: valid address accepted,
    order created and confirmed.
    """
    phone = _phone("070001")
    pnid = _pnid("070001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="HP001", product_name="Cotton Kurta", price=800.0,
        stock=10, payment_method="COD",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=1,
        customer_name="Sunita",
        # delivery_address and payment_method pending
    )

    # 1: Send a valid address
    await _msg(replay_http, phone, "42 MG Road, Pune 411001", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.delivery_address is not None, "valid address must be saved"
    assert "mg road" in conv.delivery_address.lower() or "42" in conv.delivery_address

    # 2: Confirm payment method COD
    await _msg(replay_http, phone, "COD", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.payment_method == "COD"

    # 3: Confirm order
    await _msg(replay_http, phone, "confirm", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    # Should have moved to awaiting_final_confirmation or completed
    from app.models.order import Order
    r = await replay_session.execute(
        select(Order).where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    orders = list(r.scalars().all())
    assert len(orders) >= 1, "order must be created after confirm"
    assert orders[0].delivery_address is not None
    assert "mg road" in (orders[0].delivery_address or "").lower() or "42" in (orders[0].delivery_address or "")


# ---------------------------------------------------------------------------
# Scenario 8 — Address-confirmation gate: unclear replies must escalate,
# not loop forever (FIX 2).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s8_address_confirm_unclear_escalates_not_loops(replay_http, replay_session):
    """
    Repeated unclear replies ("Hi") at the saved-address yes/change gate must
    advance the SAME slot_attempt_count used elsewhere for delivery_address
    and escalate to a human once the cap is hit — not loop indefinitely.
    """
    from app.services.order_pipeline import _SLOT_ATTEMPT_ESCALATE

    phone = _phone("080001")
    pnid = _pnid("080001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="AC001", product_name="Linen Saree", price=3200.0,
        stock=5, payment_method="COD",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_order_quantity=1,
        customer_name="Anita",
    )
    await _seed_saved_address_gate(
        replay_session, conv_id=conv_id, address="9 Lake View, Chennai 600001",
    )

    for i in range(1, _SLOT_ATTEMPT_ESCALATE):
        await _msg(replay_http, phone, "Hi", pnid=pnid)
        conv = await _get_conv(replay_session, conv_id)
        assert conv.delivery_address is None, "unclear reply must not fill the address"
        assert conv.slot_attempt_count == i, (
            f"unclear reply #{i} should bump slot_attempt_count to {i}, got {conv.slot_attempt_count}"
        )
        assert conv.ai_enabled is True, "must not escalate before the cap"

    # One more unclear reply hits the cap -> escalate to human, stop re-prompting.
    await _msg(replay_http, phone, "Hi", pnid=pnid)
    conv = await _get_conv(replay_session, conv_id)
    assert conv.ai_enabled is False, "cap hit must hand off to a human"
    assert conv.taken_over_note and "delivery_address" in conv.taken_over_note
