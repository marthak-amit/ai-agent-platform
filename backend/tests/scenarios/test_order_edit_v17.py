"""
Scenario tests (scripted planner, real webhook → Postgres) for the post-confirm order edit — conv=60 ORD-2026-0079:

  A-Line Linen Kurti Black/XL × 2 was confirmed (``pending_payment``). The customer immediately wrote "Ohh I want 1 black and 1gray"; the cart was
  empty, the planner asked a clarifying question and the bot dumped an unrelated catalogue, twice.

Now: the message is read against the order's own product (inheriting XL), the customer is asked "Update order #… to: 1× Black/XL, 1× Grey/XL — ₹1,758?"
[Update order | Keep as is], and Update edits the same order in place: lines, total, reservations, audit row, fresh payment instructions.
The planner is never consulted on these turns.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.scenarios.harness import Customer, ScriptedPlanner, capture_structured, conv_of, joined, seed_shop


@pytest.fixture
def planner(monkeypatch):
    """A scripted planner that fails the test when consulted."""
    fake = ScriptedPlanner()
    monkeypatch.setattr("app.services.agent_planner.call_model", fake)
    return fake


async def _add_linen(session, client_id: int) -> None:
    """LK10001 A-Line Linen Kurti ₹879 (Black/Grey × XL/L, Navy/XL sold out) and LK10002 Short Linen Kurti ₹699 (Black XL, Grey M/L — no Grey XL)."""
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    a = Product(client_id=client_id, name="A-Line Linen Kurti", sku="LK10001", price=879.0, stock=20, is_active=True, has_variants=True, category="Kurti")
    b = Product(client_id=client_id, name="Short Linen Kurti", sku="LK10002", price=699.0, stock=15, is_active=True, has_variants=True, category="Kurti")
    session.add_all([a, b])
    await session.flush()
    for color, size, stock in (("Black", "XL", 5), ("Black", "L", 5), ("Grey", "XL", 5), ("Grey", "L", 5), ("Navy", "XL", 0)):
        session.add(ProductVariant(product_id=a.id, client_id=client_id, color=color, size=size, stock=stock, is_active=True, price=879.0))
    for color, size, stock in (("Black", "XL", 5), ("Grey", "M", 5), ("Grey", "L", 5)):
        session.add(ProductVariant(product_id=b.id, client_id=client_id, color=color, size=size, stock=stock, is_active=True, price=699.0))
    await session.commit()
    from app.services import catalogue_snapshot

    catalogue_snapshot.invalidate(client_id)


async def _placed_order(session, client, me, *, sku="LK10001", name="A-Line Linen Kurti", price=879.0, color="Black", size="XL", qty=2, minutes_old=3):
    """The conversation exists (a greeting was sent) and ORD is confirmed: UPI, pending_payment, stock reserved."""
    from app.models.order import Order
    from app.models.product import Product
    from app.services import order_service

    await me.say("hi")
    conv = await conv_of(session, client.id, me.phone)
    product = (await session.execute(select(Product).where(Product.client_id == client.id, Product.sku == sku))).scalar_one()
    order = await order_service.create_cart_order(
        db=session, client_id=client.id, conversation_id=conv.id, customer_name="Asha Shah", customer_phone=me.phone,
        delivery_address="12 MG Road, Surat 395007", payment_method="UPI", mobile_number=me.phone,
        line_items=[{"product_id": product.id, "product_name": name, "product_sku": sku, "variant_color": color, "variant_size": size,
                     "quantity": qty, "unit_price": price}],
    )
    if minutes_old:
        row = (await session.execute(select(Order).where(Order.id == order.id).execution_options(populate_existing=True))).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
        await session.commit()
    return order


async def _order(session, order_id: int):
    """The order, fresh."""
    from app.models.order import Order

    return (await session.execute(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))).scalar_one()


async def _reservations(session, order_id: int):
    """(variant colour/size, qty, status) of every reservation of the order."""
    from app.models.product_variant import ProductVariant
    from app.models.stock_reservation import StockReservation

    rows = (await session.execute(
        select(ProductVariant.color, ProductVariant.size, StockReservation.quantity, StockReservation.status)
        .join(ProductVariant, ProductVariant.id == StockReservation.variant_id).where(StockReservation.order_id == order_id).order_by(StockReservation.id)
    )).all()
    return [tuple(r) for r in rows]


@pytest.fixture
async def shop(replay_http, replay_session, planner, monkeypatch):
    """A shop + customer + the confirmed order of the transcript."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="73")
    await _add_linen(replay_session, client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)
    order = await _placed_order(replay_session, client, me)
    return me, client, order, sent


# ── the transcript ───────────────────────────────────────────────────────────

async def test_one_black_and_one_gray_after_confirming_proposes_an_edit_with_xl_inherited(shop, planner, replay_session):
    """'Ohh I want 1 black and 1gray' → one proposal for THE order (XL inherited, 'gray' = Grey), buttons, no catalogue, no planner."""
    me, client, order, sent = shop
    out = await me.say("Ohh I want 1 black and 1gray")
    text = joined(out)
    assert f"Update order #{order.order_number} to: 1× Black/XL, 1× Grey/XL — ₹1,758?" in text, text
    assert "These are available" not in text and "Chanderi" not in text
    assert out[-1].action_ids() == ["v3_oedit_yes", "v3_oedit_no"]
    assert [t for _, t in out[-1].buttons] == ["Update order", "Keep as is"]
    assert not planner.calls, "the planner must not be consulted"
    saved = await _order(replay_session, order.id)
    assert [(li.variant_color, li.variant_size, li.quantity) for li in saved.line_items] == [("Black", "XL", 2)], "nothing changes before the customer says yes"


async def test_update_edits_the_same_order_lines_total_reservations_audit_and_resends_the_payment(shop, planner, replay_session):
    """[Update order]: Black/XL ×1 + Grey/XL ×1, same number, total recalculated, reservations swapped, audit row, new payment instructions + QR."""
    from app.models.order_audit_log import OrderAuditLog

    me, client, order, sent = shop
    await me.say("I want 1 black and 2 gray")
    out = await me.tap("v3_oedit_yes")
    text = joined(out)
    saved = await _order(replay_session, order.id)
    assert saved.order_number == order.order_number and saved.status == "pending_payment"
    assert sorted((li.variant_color, li.variant_size, li.quantity) for li in saved.line_items) == [("Black", "XL", 1), ("Grey", "XL", 2)]
    assert saved.total_amount == 3 * 879.0 and saved.quantity == 1 and saved.variant_color == "Black"
    assert f"Order #{order.order_number} updated: 1× Black/XL, 2× Grey/XL — ₹2,637." in text
    assert "NEW amount" in text and "₹2,637" in text and client.upi_id in text, text          # the payment instructions are sent again with the new amount
    assert any(o.image for o in out), "a fresh QR for the new amount"
    reservations = await _reservations(replay_session, order.id)
    assert [r for r in reservations if r[3] == "released"] == [("Black", "XL", 2, "released")]
    assert sorted(r for r in reservations if r[3] == "active") == [("Black", "XL", 1, "active"), ("Grey", "XL", 2, "active")]
    audit = (await replay_session.execute(select(OrderAuditLog).where(OrderAuditLog.order_id == order.id, OrderAuditLog.action == "order_edited_by_customer"))).scalars().all()
    assert len(audit) == 1 and audit[0].detail["total_before"] == 1758.0 and audit[0].detail["total_after"] == 2637.0
    conv = await conv_of(replay_session, client.id, me.phone)
    assert conv.last_payment_instruction_at is not None and not planner.calls


async def test_the_proposal_states_the_old_total_when_it_changes(shop):
    """(was ₹1,758) appears only when the total differs."""
    me, client, order, sent = shop
    text = joined(await me.say("1 black and 2 gray"))
    assert "₹2,637 (was ₹1,758)?" in text, text


async def test_keep_as_is_changes_nothing(shop, replay_session):
    """[Keep as is] leaves lines, total and reservations alone and says so."""
    me, client, order, sent = shop
    await me.say("1 black and 1 gray")
    out = await me.tap("v3_oedit_no")
    assert "stays as it is" in joined(out) and f"#{order.order_number}" in joined(out)
    saved = await _order(replay_session, order.id)
    assert [(li.variant_color, li.quantity) for li in saved.line_items] == [("Black", 2)]
    assert [r for r in await _reservations(replay_session, order.id)] == [("Black", "XL", 2, "active")]


async def test_a_typed_yes_updates_and_a_typed_no_keeps(shop, replay_session):
    """Instagram has no buttons: 'yes' / 'no' answer the open proposal."""
    me, client, order, sent = shop
    await me.say("1 black and 1 grey")
    assert "updated" in joined(await me.say("yes")).lower()
    assert sorted(li.variant_color for li in (await _order(replay_session, order.id)).line_items) == ["Black", "Grey"]


# ── when the colour does not come in the inherited size ──────────────────────

async def test_a_colour_without_the_inherited_size_asks_for_a_size_from_its_own_options(replay_http, replay_session, planner, monkeypatch):
    """Short Linen Kurti: Black XL exists, Grey only in M / L → 'Grey isn't available in XL. Available: M, L.' → 'M' → the proposal."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="74")
    await _add_linen(replay_session, client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)
    order = await _placed_order(replay_session, client, me, sku="LK10002", name="Short Linen Kurti", price=699.0, qty=2)
    text = joined(await me.say("1 black and 1gray"))
    assert "Grey isn't available in XL" in text and "M, L" in text, text
    assert "Update order" not in text
    text = joined(await me.say("M"))
    assert f"Update order #{order.order_number} to: 1× Black/XL, 1× Grey/M — ₹1,398?" in text, text
    await me.tap("v3_oedit_yes")
    saved = await _order(replay_session, order.id)
    assert sorted((li.variant_color, li.variant_size) for li in saved.line_items) == [("Black", "XL"), ("Grey", "M")]
    assert saved.total_amount == 1398.0 and not planner.calls


async def test_a_colour_the_product_is_not_sold_in_names_the_real_options(shop):
    """'1 black and 1 purple' → Purple isn't available for A-Line Linen Kurti. Available: Black, Grey, Navy… — no proposal, no catalogue."""
    me, client, order, sent = shop
    text = joined(await me.say("1 black and 1 purple"))
    assert "Purple isn't available for A-Line Linen Kurti" in text and "Black" in text and "Grey" in text, text
    assert "Update order" not in text and "These are available" not in text


async def test_stock_counts_the_orders_own_reservation_as_available(shop, replay_session):
    """Black/XL: 5 in stock, 2 of them reserved by THIS order → 5 can be asked for; 6 cannot."""
    me, client, order, sent = shop
    assert "Update order" in joined(await me.say("5 black and 1 grey"))
    text = joined(await me.say("6 black and 1 grey"))
    assert "Only 5 left" in text and "Update order" not in text, text


# ── what may not be edited ───────────────────────────────────────────────────

async def test_an_order_with_a_screenshot_is_never_edited_and_a_new_order_is_offered(replay_http, replay_session, planner, monkeypatch):
    """payment_submitted: 'can't change it here' + [New order]; tapping it starts a NEW cart with the asked-for options; the order is untouched."""
    from app.models.payment_proof import PaymentProof

    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="75")
    await _add_linen(replay_session, client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)
    order = await _placed_order(replay_session, client, me)
    conv = await conv_of(replay_session, client.id, phone)
    replay_session.add(PaymentProof(order_id=order.id, client_id=client.id, conversation_id=conv.id, status="pending", media_url="https://cdn.test/p.jpg", message_id=None))
    row = await _order(replay_session, order.id)
    row.status = "payment_submitted"
    await replay_session.commit()

    out = await me.say("Ohh I want 1 black and 1gray")
    text = joined(out)
    assert "payment screenshot under review" in text and "can't change it here" in text and "new order for 1× Black, 1× Grey" in text, text
    assert out[-1].action_ids() == ["v3_oedit_new"] and "Update order" not in text
    out = await me.tap("v3_oedit_new")
    cart = await me.cart()
    assert cart is not None and sorted((ln["sku"], ln.get("color")) for ln in cart.lines) == [("LK10001", "Black"), ("LK10001", "Grey")], cart.lines
    saved = await _order(replay_session, order.id)
    assert saved.status == "payment_submitted" and [(li.variant_color, li.quantity) for li in saved.line_items] == [("Black", 2)]
    assert not planner.calls


async def test_an_order_older_than_the_window_is_not_editable(replay_http, replay_session, planner, monkeypatch):
    """45 minutes old (window 30): the message is not an edit — it goes on to the planner like any other."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="76")
    await _add_linen(replay_session, client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)
    order = await _placed_order(replay_session, client, me, minutes_old=45)
    planner.script([{"actions": [], "reply_intent": "smalltalk"}])
    await me.say("1 black and 1 grey")
    assert len(planner.calls) == 1, "outside the edit window the planner reads the message"
    saved = await _order(replay_session, order.id)
    assert [(li.variant_color, li.quantity) for li in saved.line_items] == [("Black", 2)]


async def test_an_expired_proposal_is_not_applied(shop, replay_session):
    """A tap on an old button after the proposal expired says so and changes nothing."""
    me, client, order, sent = shop
    await me.say("1 black and 1 grey")
    cart = await me.cart()
    from app.models.conversation_cart import ConversationCart

    row = (await replay_session.execute(select(ConversationCart).where(ConversationCart.id == cart.id).execution_options(populate_existing=True))).scalar_one()
    state = dict(row.state)
    state["order_edit"] = {**state["order_edit"], "ts": time.time() - 3600}
    row.state = state
    await replay_session.commit()
    out = await me.tap("v3_oedit_yes")
    assert "expired" in joined(out).lower()
    assert [(li.variant_color, li.quantity) for li in (await _order(replay_session, order.id)).line_items] == [("Black", 2)]


# ── ask_clarify never dumps the catalogue ────────────────────────────────────

async def test_a_product_clarify_without_a_product_is_a_plain_question_not_a_catalogue_dump(shop, planner):
    """The planner's ``ask_clarify slot=product`` with no candidates (the transcript's reply) → 'what are you looking for?', no list."""
    me, client, order, sent = shop
    await me.say("hello")           # clears nothing; the order edit does not apply to this message
    planner.script([{"actions": [{"type": "ask_clarify", "slot": "product", "question": "Which one?", "options": []}], "reply_intent": "none"}])
    out = await me.say("the one we talked about")
    text = joined(out)
    assert "These are available" not in text and "₹" not in text, text
    assert "What are you looking for" in text


# ── the whole transcript: 4 planner calls before, 0 now ──────────────────────

async def test_the_transcript_of_conv60_needs_no_planner_call(shop, planner):
    """'Hello buddy', 'I want kurti', 'Ohh I want 1 black and 1gray', 'I want 1 black and 1 gray' — all four were planner turns in v1.6."""
    me, client, order, sent = shop
    assert "Welcome" in joined(await me.say("Hello buddy"))
    text = joined(await me.say("I want kurti"))
    assert "Linen Kurti" in text or "Cotton Kurti" in text, text
    assert "Update order" in joined(await me.say("Ohh I want 1 black and 1gray"))
    assert "Update order" in joined(await me.say("I want 1 black and 1 gray"))
    assert planner.calls == [], f"the planner was called {len(planner.calls)} time(s)"


async def test_a_paid_order_is_never_edited_and_a_new_order_is_offered(replay_http, replay_session, planner, monkeypatch):
    """The order was already approved (paid): 'already confirmed … can't change it here' + [New order] — it is not in the open-payment list, so this needs its own lookup."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="77")
    await _add_linen(replay_session, client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)
    order = await _placed_order(replay_session, client, me)
    row = await _order(replay_session, order.id)
    row.status, row.payment_status = "paid", "paid"
    await replay_session.commit()
    out = await me.say("1 black and 1gray")
    text = joined(out)
    assert "already confirmed" in text and "can't change it here" in text and "new order for 1× Black, 1× Grey" in text, text
    assert out[-1].action_ids() == ["v3_oedit_new"] and not planner.calls
    assert "updated" not in text.lower()
    saved = await _order(replay_session, order.id)
    assert [(li.variant_color, li.quantity) for li in saved.line_items] == [("Black", 2)] and saved.status == "paid"
    await me.say("yes")                                   # a typed yes answers the "new order?" offer too
    cart = await me.cart()
    assert cart is not None and len(cart.lines) == 2
