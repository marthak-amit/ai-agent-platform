"""
Golden replay: manual UPI payment verification + dashboard outbound messaging.

We never collect money — the customer pays the seller's UPI and sends a
screenshot; the seller approves/rejects it. Every scenario drives the real
HTTP surface (WhatsApp webhook + dashboard APIs) against a real Postgres and
asserts the DATABASE END STATE, not just the replies.

Scenarios:
  1. image -> approve -> paid                    (stock deducted exactly once)
  2. reject -> customer re-sends -> approve      (stock still reserved, then deducted once)
  3. "paid" text with no image                   (asks for screenshot, nothing changes)
  4. image with no pending order                 (no proof row; message + media persisted)
  5. double approve (sequential + concurrent)    (idempotent: one deduction, one message)
  6. dashboard send outside 24h                  (409 WINDOW_CLOSED + templates, nothing sent)
  7. bot paused                                  (no AI reply; approve still sends its template)
  8. payment step                                (instruction text + QR + reservation)
  9. cancel                                      (reservation released, stock untouched)
 10. no UPI configured                           (payment step blocked + dashboard alert)

Skipped automatically when local Postgres is not reachable.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import send_button, send_image, send_message, stub_media_and_capture_sends

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


def _phone(s: str) -> str:
    return f"91950000{s}"


def _pnid(s: str) -> str:
    return f"777{s}"


# ── seeding / reading helpers ────────────────────────────────────────────────

async def _seed(session: AsyncSession, suffix: str, *, stock: int = 10, upi: bool = True):
    """Client (+owner user) and product; returns (client, product, user, phone, pnid)."""
    from app.models.user import User

    phone, pnid = _phone(suffix), _pnid(suffix)
    client, product = await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=pnid, product_sku=f"PV{suffix}",
        product_name="Proof Kurta", price=650.0, stock=stock, payment_method="UPI" if upi else "COD",
    )
    user = User(client_id=client.id, email=f"owner{suffix}@replay.test", hashed_password="x",
                role="owner", permissions=[])
    session.add(user)
    await session.commit()
    await session.refresh(user)
    user.client = client
    return client, product, user, phone, pnid


async def _pending_order(session: AsyncSession, client, product, phone: str, *, qty: int = 2,
                         stage: str = "payment", ai_enabled: bool = True):
    """An UPI order in pending_payment (with its stock reservation) + its conversation."""
    from app.models.conversation import Conversation
    from app.services import order_service

    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=client.id, current_stage=stage,
        customer_name="Asha Shah", delivery_address="12 MG Road, Surat", ai_enabled=ai_enabled,
        payment_method="UPI", pending_product_sku=product.sku, pending_order_quantity=qty,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    order = await order_service.create_order(
        db=session, client_id=client.id, customer_name="Asha Shah", customer_phone=phone,
        delivery_address="12 MG Road, Surat", product_name=product.name, quantity=qty,
        unit_price=product.price, payment_method="UPI", product_id=product.id,
        product_sku=product.sku, conversation_id=conv.id,
    )
    return conv, order


async def _fresh(session: AsyncSession, model, pk: int):
    r = await session.execute(select(model).where(model.id == pk).execution_options(populate_existing=True))
    return r.scalar_one()


async def _all(session: AsyncSession, model, *where, order=None):
    stmt = select(model).where(*where).execution_options(populate_existing=True)
    if order is not None:
        stmt = stmt.order_by(order)
    return list((await session.execute(stmt)).scalars().all())


@pytest.fixture
def seller(replay_http):
    """Authenticate dashboard calls as a given user (bypasses JWT; permissions already satisfied)."""
    from app.main import app
    from app.routers import conversations, payment_verification
    from app.routers.auth import get_current_client, get_owner_client

    deps = [payment_verification._perm, conversations._perm, get_current_client, get_owner_client]
    holder: dict = {}

    def _login(user):
        async def _user():
            return user

        async def _client():
            return user.client

        app.dependency_overrides[payment_verification._perm] = _user
        app.dependency_overrides[conversations._perm] = _user
        app.dependency_overrides[get_current_client] = _client
        app.dependency_overrides[get_owner_client] = _client
        holder["user"] = user

    yield _login
    for d in deps:
        app.dependency_overrides.pop(d, None)


# ── 1. image -> approve -> paid ──────────────────────────────────────────────

async def test_image_then_approve_marks_paid_and_deducts_stock_once(replay_http, replay_session, seller, monkeypatch):
    """Screenshot -> payment_submitted; approve -> paid, stock 10 -> 8 once, customer told, audit written."""
    from app.models.message import Message
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.models.payment_proof import PaymentProof
    from app.models.product import Product
    from app.models.stock_reservation import StockReservation

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0001")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    seller(user)
    pid, oid = product.id, order.id

    # Reservation exists, nothing deducted yet.
    res = await _all(replay_session, StockReservation, StockReservation.order_id == oid)
    assert [(r.quantity, r.status) for r in res] == [(2, "active")]
    assert (await _fresh(replay_session, Product, pid)).stock == 10

    resp = await send_image(replay_http, phone, phone_number_id=pnid)
    assert resp.status_code == 200, resp.text

    order = await _fresh(replay_session, Order, oid)
    assert order.status == "payment_submitted" and order.payment_submitted_at is not None
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)
    assert len(proofs) == 1 and proofs[0].status == "pending"
    assert proofs[0].media_url.startswith("https://cdn.test/chat/")
    assert proofs[0].client_id == client.id and proofs[0].conversation_id == conv.id
    msg = await _fresh(replay_session, Message, proofs[0].message_id)
    assert (msg.direction, msg.sender_type, msg.channel, msg.media_type) == ("inbound", "customer", "whatsapp", "image")
    assert msg.media_url == proofs[0].media_url
    assert sent["texts"][-1] == "Thanks! Our team will verify your payment shortly and update you here."
    assert (await _fresh(replay_session, Product, pid)).stock == 10, "stock must not move before approval"

    # Dashboard lists it.
    listing = (await replay_http.get("/payments/pending")).json()
    assert [r["order_id"] for r in listing] == [oid] and listing[0]["amount_expected"] == 1300.0
    assert (await replay_http.get("/payments/pending/count")).json() == {"count": 1}

    resp = await replay_http.post(f"/orders/{oid}/payment/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid" and resp.json()["changed"] is True

    order = await _fresh(replay_session, Order, oid)
    assert order.status == "paid" and order.payment_status == "paid"
    assert order.stock_deducted is True and order.paid_at is not None
    assert (await _fresh(replay_session, Product, pid)).stock == 8
    proof = (await _all(replay_session, PaymentProof, PaymentProof.order_id == oid))[0]
    assert proof.status == "approved" and proof.reviewed_by == user.id
    assert proof.reviewed_by_name == user.email and proof.reviewed_at is not None
    res = await _all(replay_session, StockReservation, StockReservation.order_id == oid)
    assert [r.status for r in res] == ["consumed"]
    assert f"Payment confirmed ✅ Your order #{order.order_number} is confirmed." in sent["texts"]
    audit = await _all(replay_session, OrderAuditLog, OrderAuditLog.order_id == oid, order=OrderAuditLog.id)
    assert [(a.action, a.from_status, a.to_status) for a in audit] == [
        ("payment_submitted", "pending_payment", "payment_submitted"),
        ("payment_approved", "payment_submitted", "paid"),
    ]
    assert audit[1].actor_user_id == user.id and audit[1].actor_name == user.email
    # Outbound confirmation persisted as a system message.
    outs = await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "outbound")
    assert any(m.sender_type == "system" and "Payment confirmed" in m.content for m in outs)


# ── 2. reject -> resend -> approve ───────────────────────────────────────────

async def test_reject_resend_approve(replay_http, replay_session, seller, monkeypatch):
    """Reject returns to pending_payment (stock still reserved); a re-sent screenshot is approved -> paid once."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof
    from app.models.product import Product
    from app.models.stock_reservation import StockReservation

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0002")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=3)
    seller(user)
    pid, oid = product.id, order.id

    await send_image(replay_http, phone, phone_number_id=pnid)
    resp = await replay_http.post(f"/orders/{oid}/payment/reject", json={"reason": "Amount mismatch"})
    assert resp.status_code == 200 and resp.json()["status"] == "pending_payment"

    order = await _fresh(replay_session, Order, oid)
    assert order.status == "pending_payment" and order.stock_deducted is False
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid, order=PaymentProof.id)
    assert [(p.status, p.reject_reason) for p in proofs] == [("rejected", "Amount mismatch")]
    assert proofs[0].reviewed_by == user.id and proofs[0].reviewed_at is not None
    assert "We couldn't confirm your payment: Amount mismatch. Please check and send the screenshot again." in sent["texts"]
    assert (await _fresh(replay_session, Product, pid)).stock == 10
    res = await _all(replay_session, StockReservation, StockReservation.order_id == oid)
    assert [r.status for r in res] == ["active"], "stock stays reserved after a reject"
    assert (await replay_http.get("/payments/pending")).json() == []
    rejected = (await replay_http.get("/payments/pending?status=rejected")).json()
    assert rejected[0]["reject_reason"] == "Amount mismatch" and rejected[0]["reviewed_by"] == user.email

    # Customer re-sends a clearer screenshot.
    await send_image(replay_http, phone, media_id="media.proof.2", phone_number_id=pnid)
    order = await _fresh(replay_session, Order, oid)
    assert order.status == "payment_submitted"
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid, order=PaymentProof.id)
    assert [p.status for p in proofs] == ["rejected", "pending"]

    resp = await replay_http.post(f"/orders/{oid}/payment/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "paid"
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid, order=PaymentProof.id)
    assert [p.status for p in proofs] == ["rejected", "approved"]
    assert (await _fresh(replay_session, Product, pid)).stock == 7
    assert (await _fresh(replay_session, Order, oid)).stock_deducted is True


# ── 3. "paid" with no image ──────────────────────────────────────────────────

async def test_paid_text_without_image_asks_for_screenshot(replay_http, replay_session, monkeypatch):
    """'paid' / 'payment kar diya' never marks anything paid; the bot asks for the screenshot."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof
    from app.models.product import Product

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0003")
    conv, order = await _pending_order(replay_session, client, product, phone)
    oid, pid = order.id, product.id

    for i, text in enumerate(["paid", "payment kar diya"]):
        resp = await send_message(replay_http, phone, text, wamid=f"wamid.pv3.{i}.{time.time_ns()}", phone_number_id=pnid)
        assert resp.status_code == 200, resp.text

    assert sent["texts"].count("Please send the payment screenshot here so we can verify it. 📸") == 2
    order = await _fresh(replay_session, Order, oid)
    assert order.status == "pending_payment" and order.stock_deducted is False and order.paid_at is None
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == oid) == []
    assert (await _fresh(replay_session, Product, pid)).stock == 10


# ── 4. image with no pending order ───────────────────────────────────────────

async def test_image_with_no_pending_order_creates_no_proof(replay_http, replay_session, monkeypatch):
    """A random image outside the payment flow is stored as a message but never becomes a proof."""
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0004")

    resp = await send_image(replay_http, phone, phone_number_id=pnid, wamid="wamid.pv4.img")
    assert resp.status_code == 200, resp.text

    assert await _all(replay_session, PaymentProof) == []
    conv = (await _all(replay_session, Conversation, Conversation.phone_number == phone))[0]
    msgs = await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "inbound")
    assert len(msgs) == 1
    assert msgs[0].media_type == "image" and msgs[0].media_url.startswith("https://cdn.test/chat/")
    assert msgs[0].channel == "whatsapp" and msgs[0].sender_type == "customer"


# ── 5. idempotent approve ────────────────────────────────────────────────────

async def test_double_approve_is_idempotent(replay_http, replay_session, seller, monkeypatch):
    """Sequential double-click: second approve is changed=False, no second deduction or message."""
    from app.models.order_audit_log import OrderAuditLog
    from app.models.product import Product

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0005")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    seller(user)
    oid, pid = order.id, product.id
    await send_image(replay_http, phone, phone_number_id=pnid)

    first = await replay_http.post(f"/orders/{oid}/payment/approve")
    second = await replay_http.post(f"/orders/{oid}/payment/approve")
    assert first.json()["changed"] is True and second.json()["changed"] is False
    assert second.status_code == 200 and second.json()["status"] == "paid"
    assert (await _fresh(replay_session, Product, pid)).stock == 8
    assert sum("Payment confirmed" in t for t in sent["texts"]) == 1
    audit = await _all(replay_session, OrderAuditLog, OrderAuditLog.order_id == oid, OrderAuditLog.action == "payment_approved")
    assert len(audit) == 1


async def test_concurrent_approves_deduct_stock_once(replay_http, replay_session, seller, monkeypatch):
    """Two simultaneous approves serialise on the order row lock: exactly one wins."""
    from app.models.product import Product

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0006")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=4)
    seller(user)
    oid, pid = order.id, product.id
    await send_image(replay_http, phone, phone_number_id=pnid)

    a, b = await asyncio.gather(
        replay_http.post(f"/orders/{oid}/payment/approve"),
        replay_http.post(f"/orders/{oid}/payment/approve"),
    )
    assert sorted([a.json()["changed"], b.json()["changed"]]) == [False, True]
    assert (await _fresh(replay_session, Product, pid)).stock == 6
    assert sum("Payment confirmed" in t for t in sent["texts"]) == 1


async def test_approve_without_screenshot_is_rejected_409(replay_http, replay_session, seller, monkeypatch):
    """Approving a still-pending_payment order (no proof) → 409 INVALID_STATE, stock untouched."""
    from app.models.product import Product

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0007")
    conv, order = await _pending_order(replay_session, client, product, phone)
    seller(user)
    resp = await replay_http.post(f"/orders/{order.id}/payment/approve")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "INVALID_STATE"
    assert (await _fresh(replay_session, Product, product.id)).stock == 10


async def test_extra_images_while_submitted_add_proofs_and_ack_once(replay_http, replay_session, monkeypatch):
    """Three screenshots -> three proof rows; only ONE 'Received, verification in progress.' inside 10 min."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0008")
    conv, order = await _pending_order(replay_session, client, product, phone)
    for i in range(3):
        await send_image(replay_http, phone, media_id=f"media.{i}", phone_number_id=pnid)
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == order.id)
    assert len(proofs) == 3 and all(p.status == "pending" for p in proofs)
    assert (await _fresh(replay_session, Order, order.id)).status == "payment_submitted"
    # first image got the 'will verify' ack (and stamped the throttle) -> extras are silent within 10 min
    assert sent["texts"].count("Thanks! Our team will verify your payment shortly and update you here.") == 1
    assert sent["texts"].count("Received, verification in progress.") == 0


# ── 6. dashboard send outside 24h ────────────────────────────────────────────

async def _conv_with_last_inbound(session, client, phone, *, hours_ago: float):
    from app.models.conversation import Conversation
    from app.models.message import Message

    conv = Conversation(phone_number=phone, channel="whatsapp", client_id=client.id, current_stage="greeting")
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    session.add(Message(
        conversation_id=conv.id, role="user", content="hi",
        direction="inbound", sender_type="customer", channel="whatsapp",
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    ))
    await session.commit()
    return conv


async def test_dashboard_send_outside_24h_returns_window_closed(replay_http, replay_session, seller, monkeypatch):
    """Last inbound 30h ago: free-form is refused with WINDOW_CLOSED + approved templates; nothing is sent or stored."""
    from app.models.message import Message
    from app.models.message_template import MessageTemplate

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0009")
    conv = await _conv_with_last_inbound(replay_session, client, phone, hours_ago=30)
    replay_session.add_all([
        MessageTemplate(client_id=client.id, name="order_update", language="en", category="utility", status="approved", body="Hi {{1}}, update"),
        MessageTemplate(client_id=client.id, name="promo", language="en", category="marketing", status="approved", body="sale"),
        MessageTemplate(client_id=client.id, name="pending_one", language="en", category="utility", status="pending", body="x"),
    ])
    await replay_session.commit()
    seller(user)

    resp = await replay_http.post(f"/conversations/{conv.id}/messages", json={"text": "Where is my order?"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "WINDOW_CLOSED"
    assert [t["name"] for t in detail["templates"]] == ["order_update"], "only approved UTILITY templates are offered"
    assert sent["texts"] == []
    assert await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "outbound") == []
    from app.models.conversation import Conversation
    assert (await _fresh(replay_session, Conversation, conv.id)).ai_enabled is True

    win = (await replay_http.get(f"/conversations/{conv.id}")).json()["window"]
    assert win["open"] is False and win["seconds_left"] == 0


async def test_dashboard_send_inside_window_persists_human_message_and_pauses_bot(replay_http, replay_session, seller, monkeypatch):
    """In-window send: delivered, stored outbound/human with staff id, bot paused (human_send), retry is idempotent."""
    from app.models.conversation import Conversation
    from app.models.message import Message

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0010")
    conv = await _conv_with_last_inbound(replay_session, client, phone, hours_ago=2)
    seller(user)

    body = {"text": "Your order ships today 🚚", "client_msg_id": "abc-123"}
    resp = await replay_http.post(f"/conversations/{conv.id}/messages", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["bot_paused"] is True and resp.json()["message"]["sender_type"] == "human"
    retry = await replay_http.post(f"/conversations/{conv.id}/messages", json=body)
    assert retry.status_code == 201 and retry.json()["message"]["id"] == resp.json()["message"]["id"]
    assert sent["texts"] == ["Your order ships today 🚚"], "a retried client_msg_id must not double-send"

    outs = await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "outbound")
    assert len(outs) == 1
    assert (outs[0].sender_type, outs[0].sender_user_id, outs[0].channel) == ("human", user.id, "whatsapp")
    c = await _fresh(replay_session, Conversation, conv.id)
    assert c.ai_enabled is False and c.bot_pause_source == "human_send" and c.human_last_activity_at is not None

    win = (await replay_http.get(f"/conversations/{conv.id}")).json()["window"]
    assert win["open"] is True and 0 < win["seconds_left"] <= 22 * 3600


async def test_dashboard_send_respects_opt_out(replay_http, replay_session, seller, monkeypatch):
    """Opted-out customer: 409 OPTED_OUT, nothing sent."""
    from app.models.customer import Customer

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0011")
    conv = await _conv_with_last_inbound(replay_session, client, phone, hours_ago=1)
    replay_session.add(Customer(client_id=client.id, phone=phone, opted_out=True))
    await replay_session.commit()
    seller(user)
    resp = await replay_http.post(f"/conversations/{conv.id}/messages", json={"text": "hello"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "OPTED_OUT"
    assert sent["texts"] == []


async def test_conversations_are_tenant_scoped(replay_http, replay_session, seller, monkeypatch):
    """A seller can neither list nor open another business's conversation."""
    stub_media_and_capture_sends(monkeypatch)
    c1, p1, u1, ph1, _ = await _seed(replay_session, "0012")
    c2, p2, u2, ph2, _ = await _seed(replay_session, "0013")
    conv2 = await _conv_with_last_inbound(replay_session, c2, ph2, hours_ago=1)
    seller(u1)
    assert (await replay_http.get("/conversations")).json() == []
    assert (await replay_http.get(f"/conversations/{conv2.id}")).status_code == 404
    assert (await replay_http.post(f"/conversations/{conv2.id}/messages", json={"text": "x"})).status_code == 404


async def test_inbox_filters_unread_payment_and_bot_paused(replay_http, replay_session, seller, monkeypatch):
    """List filters: unread / payment_to_verify / bot_paused, plus read marker."""
    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0014")
    conv, order = await _pending_order(replay_session, client, product, phone)
    seller(user)
    await send_image(replay_http, phone, phone_number_id=pnid)

    def ids(rows):
        return [r["id"] for r in rows]

    assert ids((await replay_http.get("/conversations?filter=payment_to_verify")).json()) == [conv.id]
    unread = (await replay_http.get("/conversations?filter=unread")).json()
    assert ids(unread) == [conv.id] and unread[0]["unread_count"] >= 1
    assert (await replay_http.get("/conversations?filter=bot_paused")).json() == []
    assert ids((await replay_http.get(f"/conversations?search={phone[-6:]}")).json()) == [conv.id]
    assert (await replay_http.get("/conversations?search=zzzz")).json() == []

    assert (await replay_http.post(f"/conversations/{conv.id}/read")).status_code == 204
    assert (await replay_http.get("/conversations?filter=unread")).json() == []

    await replay_http.patch(f"/conversations/{conv.id}/takeover", json={"note": "handling"})
    assert ids((await replay_http.get("/conversations?filter=bot_paused")).json()) == [conv.id]

    detail = (await replay_http.get(f"/conversations/{conv.id}")).json()
    proof_msgs = [m for m in detail["messages"] if m["payment_proof"]]
    assert len(proof_msgs) == 1
    card = proof_msgs[0]["payment_proof"]
    assert card["order_id"] == order.id and card["amount_expected"] == 1300.0 and card["status"] == "pending"
    assert detail["current_order"]["id"] == order.id
    assert {e["status"] for e in detail["events"]} >= {"order_created", "payment_submitted"}


# ── 7. bot paused ────────────────────────────────────────────────────────────

async def test_bot_paused_skips_reply_but_approve_still_sends_template(replay_http, replay_session, seller, monkeypatch):
    """Paused: customer chat gets NO bot reply (message still stored); proof recorded silently; approve still messages."""
    from app.models.message import Message
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0015")
    conv, order = await _pending_order(replay_session, client, product, phone, ai_enabled=False)
    seller(user)

    resp = await send_message(replay_http, phone, "do you have this in red?", wamid="wamid.pv15.txt", phone_number_id=pnid)
    assert resp.status_code == 200
    assert sent["texts"] == [], "no bot reply while paused"
    inbound = await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "inbound")
    assert [m.content for m in inbound] == ["do you have this in red?"]
    assert await _all(replay_session, Message, Message.conversation_id == conv.id, Message.direction == "outbound") == []

    await send_image(replay_http, phone, phone_number_id=pnid)
    assert sent["texts"] == [], "proof acknowledgement is also silent while paused"
    assert (await _fresh(replay_session, Order, order.id)).status == "payment_submitted"
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == order.id)) == 1

    resp = await replay_http.post(f"/orders/{order.id}/payment/approve")
    assert resp.status_code == 200
    assert [t for t in sent["texts"] if "Payment confirmed" in t], "approve template ignores the pause"


async def test_human_pause_auto_resumes_after_idle(replay_http, replay_session, monkeypatch):
    """A human_send pause idle past bot_auto_resume_minutes resumes on the next inbound; manual pauses never do."""
    from app.models.conversation import Conversation

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0016")
    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=client.id, current_stage="greeting", ai_enabled=False,
        bot_pause_source="human_send", bot_paused_at=datetime.now(timezone.utc) - timedelta(minutes=45),
        human_last_activity_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    replay_session.add(conv)
    await replay_session.commit()
    await send_message(replay_http, phone, "hello", wamid="wamid.pv16.a", phone_number_id=pnid)
    assert (await _fresh(replay_session, Conversation, conv.id)).ai_enabled is True

    conv2_phone = _phone("0017")
    c2 = Conversation(
        phone_number=conv2_phone, channel="whatsapp", client_id=client.id, current_stage="greeting", ai_enabled=False,
        bot_pause_source="manual", bot_paused_at=datetime.now(timezone.utc) - timedelta(hours=5),
        human_last_activity_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    replay_session.add(c2)
    await replay_session.commit()
    await send_message(replay_http, conv2_phone, "hello", wamid="wamid.pv16.b", phone_number_id=pnid)
    assert (await _fresh(replay_session, Conversation, c2.id)).ai_enabled is False


# ── 8. payment step: instruction + QR + reservation ─────────────────────────

async def test_confirming_order_sends_upi_instruction_qr_and_reserves_stock(replay_http, replay_session, monkeypatch):
    """Confirm & Pay -> pending_payment order, stock reserved (not deducted), summary+amount+UPI text, QR, closing line last."""
    from app.models.conversation import Conversation
    from app.models.order import Order
    from app.models.product import Product
    from app.models.stock_reservation import StockReservation

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0018")
    client.upi_display_name = "Riya Sarees"
    await replay_session.commit()
    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=client.id, current_stage="awaiting_final_confirmation",
        pending_product_sku=product.sku, pending_order_quantity=2, customer_name="Asha Shah",
        delivery_address="12 MG Road, Surat", mobile_number=phone, payment_method="UPI", summary_shown=True,
    )
    replay_session.add(conv)
    await replay_session.commit()

    resp = await send_button(replay_http, phone, "confirm_pay", "Confirm & Pay",
                             wamid=f"wamid.pv18.{time.time_ns()}", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text

    orders = await _all(replay_session, Order, Order.conversation_id == conv.id)
    assert len(orders) == 1 and orders[0].status == "pending_payment" and orders[0].stock_deducted is False
    res = await _all(replay_session, StockReservation, StockReservation.order_id == orders[0].id)
    assert [(r.quantity, r.status) for r in res] == [(2, "active")]
    assert (await _fresh(replay_session, Product, product.id)).stock == 10

    instruction = next(t for t in sent["texts"] if "UPI" in t)
    assert orders[0].order_number in instruction and "₹1,300" in instruction
    assert "test@upi" in instruction and "Riya Sarees" in instruction
    assert sent["images"], "UPI QR image must be sent"
    qr_url, caption = sent["images"][-1]
    assert qr_url.startswith("https://cdn.test/payment-qr/")
    assert caption == "After payment, please send the payment screenshot here."
    conv = await _fresh(replay_session, Conversation, conv.id)
    assert conv.current_stage == "payment"


async def test_reservation_blocks_overselling_the_last_pieces(replay_http, replay_session, monkeypatch):
    """Another customer's unpaid order holds stock: a second customer can't order those same units."""
    from app.models.conversation import Conversation
    from app.models.order import Order

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0019", stock=3)
    await _pending_order(replay_session, client, product, _phone("0019"), qty=2)   # holds 2 of 3

    phone2 = _phone("0020")
    conv = Conversation(
        phone_number=phone2, channel="whatsapp", client_id=client.id, current_stage="awaiting_final_confirmation",
        pending_product_sku=product.sku, pending_order_quantity=2, customer_name="Ben", delivery_address="Pune",
        mobile_number=phone2, payment_method="UPI", summary_shown=True,
    )
    replay_session.add(conv)
    await replay_session.commit()
    await send_button(replay_http, phone2, "confirm_pay", "Confirm & Pay",
                      wamid=f"wamid.pv19.{time.time_ns()}", phone_number_id=pnid)
    assert await _all(replay_session, Order, Order.conversation_id == conv.id) == []


# ── 9. cancel / expiry ───────────────────────────────────────────────────────

async def test_cancel_releases_reservation_without_touching_stock(replay_http, replay_session, seller, monkeypatch):
    """Seller cancel: order cancelled, reservation released, stock unchanged, customer informed, audit written."""
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.models.product import Product
    from app.models.stock_reservation import StockReservation

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0021")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    seller(user)
    await send_image(replay_http, phone, phone_number_id=pnid)   # payment_submitted -> cancelled is also allowed

    resp = await replay_http.post(f"/orders/{order.id}/payment/cancel", json={"reason": "Out of stock"})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    again = await replay_http.post(f"/orders/{order.id}/payment/cancel")
    assert again.json()["changed"] is False

    o = await _fresh(replay_session, Order, order.id)
    assert o.status == "cancelled" and o.cancel_reason == "Out of stock" and o.cancelled_at is not None
    assert (await _fresh(replay_session, Product, product.id)).stock == 10 and o.stock_deducted is False
    res = await _all(replay_session, StockReservation, StockReservation.order_id == order.id)
    assert [r.status for r in res] == ["released"]
    assert sum("cancelled" in t for t in sent["texts"]) == 1
    audit = await _all(replay_session, OrderAuditLog, OrderAuditLog.order_id == order.id, OrderAuditLog.action == "order_cancelled")
    assert len(audit) == 1 and audit[0].detail["reason"] == "Out of stock"
    # a paid order can't be cancelled through this flow
    resp = await replay_http.post(f"/orders/{order.id}/payment/approve")
    assert resp.status_code == 409


async def test_expiry_job_cancels_only_unpaid_pending_orders(replay_http, replay_session, monkeypatch):
    """expire_stale_orders: old pending_payment -> cancelled (reservation released); payment_submitted is left for the seller."""
    from app.models.order import Order
    from app.models.stock_reservation import StockReservation
    from app.services import payment_verification_service as pvs

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0022")
    _, stale = await _pending_order(replay_session, client, product, phone, qty=1)
    _, awaiting = await _pending_order(replay_session, client, product, _phone("0023"), qty=1)
    awaiting.status = "payment_submitted"
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    stale.created_at = old
    awaiting.created_at = old
    await replay_session.commit()

    assert await pvs.expire_stale_orders(replay_session) == 1
    assert (await _fresh(replay_session, Order, stale.id)).status == "cancelled"
    assert (await _fresh(replay_session, Order, awaiting.id)).status == "payment_submitted"
    res = await _all(replay_session, StockReservation, StockReservation.order_id == stale.id)
    assert [r.status for r in res] == ["released"]


# ── 10. missing UPI ──────────────────────────────────────────────────────────

async def test_missing_upi_blocks_payment_step_and_raises_alert(replay_http, replay_session, monkeypatch):
    """No UPI ID: no order is created, the customer is told payment is being set up, and the dashboard alert is stamped."""
    from app.models.client import Client
    from app.models.conversation import Conversation
    from app.models.order import Order

    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0024")
    client.upi_id = None
    client.accepts_cod = False
    await replay_session.commit()
    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=client.id, current_stage="awaiting_final_confirmation",
        pending_product_sku=product.sku, pending_order_quantity=1, customer_name="Asha", delivery_address="Surat",
        mobile_number=phone, payment_method="UPI", summary_shown=True,
    )
    replay_session.add(conv)
    await replay_session.commit()

    await send_button(replay_http, phone, "confirm_pay", "Confirm & Pay",
                      wamid=f"wamid.pv24.{time.time_ns()}", phone_number_id=pnid)
    assert await _all(replay_session, Order, Order.conversation_id == conv.id) == []
    assert any("being set up" in t for t in sent["texts"])
    assert (await _fresh(replay_session, Client, client.id)).payment_setup_alert_at is not None
    assert (await _fresh(replay_session, Conversation, conv.id)).current_stage != "payment"
