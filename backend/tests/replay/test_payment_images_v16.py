"""
Replay (real webhook → real Postgres) for the v1.6 payment-image fixes of conv=60 (ORD-2026-0077 / 0078 / 0068):

  1. an image the OCR cannot read (dark-mode UPI screenshot) is NEVER taken for a product photo — the customer is asked, once per burst;
  2. "This is payment photo" files the unfiled images as proofs (audited, reclassified_by_customer) without the planner;
  3. a receipt whose ₹ amount equals the one unpaid order's total is filed by itself (₹8,548 / lakh grouping / OCR spacing);
  4. the order picker lists only orders waiting for money (list message beyond 3), a stale picker tap is explained;
  5. "Please confirm my payments" is a per-order status, no planner.

OCR / vision / downloads are scripted; nothing touches the network.
"""

from __future__ import annotations

import time
import unittest.mock as mock

import pytest

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_button, send_image, send_message, stub_media_and_capture_sends
from tests.replay.test_payment_proof_hardening import _last_buttons, _ocr, _receipt, _second_order
from tests.replay.test_payment_verification_replay import _all, _fresh, _pending_order, _seed  # noqa: F401

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


def _no_planner(monkeypatch):
    """The planner must not be consulted on these paths."""
    async def _boom(*_a, **_k):
        """Fail the test if the planner is called."""
        raise AssertionError("the planner was called")

    monkeypatch.setattr("app.services.agent_planner.call_model", _boom)


async def _set_total(session, order_id, total):
    """Give an order an exact total (₹8,548 in the transcript)."""
    from app.models.order import Order

    row = await _fresh(session, Order, order_id)
    row.total_amount = total
    await session.commit()


# ── 1 + 2: dark-mode screenshots, the burst, the customer's correction ───────

async def test_unreadable_screenshots_are_asked_once_and_the_customers_correction_files_them(replay_http, replay_session, monkeypatch):
    """TEST 1 + 3 + 4: three dark-mode screenshots in a row (OCR empty) → no product-photo reply, ONE question; 'This is payment photo' → all
    three become payment proofs (reclassified_by_customer), the order moves to payment_submitted, and no planner call happens."""
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, "")
    _no_planner(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "1601")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    oid = order.id

    for i in range(3):
        resp = await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.dark.{i}.{time.time_ns()}")
        assert resp.status_code == 200, resp.text
    from app.services import whatsapp_service

    assert whatsapp_service._raw_send_button_message.await_count == 1, "three images in a burst → ONE question"
    body, buttons = _last_buttons()
    assert [t for _, t in buttons] == ["Payment screenshot", "Product photo"]
    assert not any("Nice photo" in t for t in sent["texts"])
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == oid) == []
    from app.models.conversation import Conversation

    assert (await _fresh(replay_session, Conversation, conv.id)).pending_proof_image_id is not None

    resp = await send_message(replay_http, phone, "This is payment photo", phone_number_id=pnid, wamid=f"wamid.fix.{time.time_ns()}")
    assert resp.status_code == 200, resp.text
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)
    assert len(proofs) == 3 and all(p.reclassified_by_customer and p.status == "pending" for p in proofs)
    assert (await _fresh(replay_session, Order, oid)).status == "payment_submitted"
    actions = [a.action for a in await _all(replay_session, OrderAuditLog, OrderAuditLog.order_id == oid, order=OrderAuditLog.id)]
    assert actions.count("proof_reclassified_by_customer") == 3 and "payment_submitted" in actions
    assert sent["texts"][-1].startswith("Thanks!")
    assert (await _fresh(replay_session, Conversation, conv.id)).pending_proof_image_id is None


async def test_payment_affirmation_without_any_image_asks_for_the_screenshot(replay_http, replay_session, monkeypatch):
    """'already paid' with nothing to file → 'Please send the payment screenshot here', never the planner."""
    sent = stub_media_and_capture_sends(monkeypatch)
    _no_planner(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "1602")
    await _pending_order(replay_session, client, product, phone)
    await send_message(replay_http, phone, "already paid", phone_number_id=pnid, wamid=f"wamid.paid.{time.time_ns()}")
    assert "send the payment screenshot" in sent["texts"][-1]


async def test_the_product_photo_button_for_an_earlier_image_files_nothing(replay_http, replay_session, monkeypatch):
    """After 'Product photo' the pending image is cleared and no proof exists."""
    from app.models.conversation import Conversation
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, "")
    client, product, user, phone, pnid = await _seed(replay_session, "1603")
    conv, order = await _pending_order(replay_session, client, product, phone)
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.p.{time.time_ns()}")
    _, buttons = _last_buttons()
    prod = next(i for i, _ in buttons if i.startswith("v3_proof_product"))
    await send_button(replay_http, phone, prod, "Product photo", phone_number_id=pnid)
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == order.id) == []
    assert (await _fresh(replay_session, Conversation, conv.id)).pending_proof_image_id is None


# ── 3: amount match → auto attach ────────────────────────────────────────────

@pytest.mark.parametrize("ocr_amount", ["8,548", "8 548", "8,548.00"])
async def test_receipt_amount_matching_the_single_unpaid_order_is_filed_by_itself(replay_http, replay_session, monkeypatch, ocr_amount):
    """TEST 6: one unpaid order of ₹8,548 + a PhonePe receipt for ₹8,548 (any OCR spacing) → proof, order payment_submitted, no question."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    suffix = {"8,548": "1611", "8 548": "1612", "8,548.00": "1613"}[ocr_amount]
    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt(ocr_amount))
    client, product, user, phone, pnid = await _seed(replay_session, suffix)
    conv, order = await _pending_order(replay_session, client, product, phone)
    await _set_total(replay_session, order.id, 8548.0)
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.amt.{time.time_ns()}")
    assert (await _fresh(replay_session, Order, order.id)).status == "payment_submitted"
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == order.id)) == 1
    assert sent["texts"][-1].startswith("Thanks!")


# ── 4: picker ────────────────────────────────────────────────────────────────

async def test_picker_lists_only_unpaid_orders_and_uses_a_list_beyond_three(replay_http, replay_session, monkeypatch):
    """TEST 7: five open orders, one of them already payment_submitted; an ambiguous receipt → a LIST of the four unpaid ones (newest first)."""
    from app.models.order import Order

    stub_media_and_capture_sends(monkeypatch)
    lists = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", lists)
    _ocr(monkeypatch, _receipt("777"))
    client, product, user, phone, pnid = await _seed(replay_session, "1621", stock=50)
    conv, first = await _pending_order(replay_session, client, product, phone, qty=1)
    others = [await _second_order(replay_session, client, product, conv.id, phone, q) for q in (2, 3, 4, 5)]
    row = await _fresh(replay_session, Order, first.id)
    row.status = "payment_submitted"
    await replay_session.commit()
    expected = {o.order_number for o in others}

    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.pick.{time.time_ns()}")
    assert lists.await_count == 1, "more than three candidates → a list message"
    call = lists.call_args
    sections = call.kwargs["sections"] if "sections" in call.kwargs else call.args[4]
    rows = [r for section in sections for r in section["rows"]]
    assert {r["title"] for r in rows} == expected and first.order_number not in {r["title"] for r in rows}
    assert all(r["id"].startswith("v3_proof_order:") or "v3_proof_order" in r["id"] for r in rows)


async def test_two_candidates_use_buttons_and_never_include_an_order_under_review(replay_http, replay_session, monkeypatch):
    """Two unpaid + one under review → two buttons."""
    from app.models.order import Order

    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("777"))
    client, product, user, phone, pnid = await _seed(replay_session, "1622", stock=50)
    conv, reviewed = await _pending_order(replay_session, client, product, phone, qty=1)
    a = await _second_order(replay_session, client, product, conv.id, phone, 2)
    b = await _second_order(replay_session, client, product, conv.id, phone, 3)
    row = await _fresh(replay_session, Order, reviewed.id)
    row.status = "payment_submitted"
    await replay_session.commit()
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.pick2.{time.time_ns()}")
    _, buttons = _last_buttons()
    assert {t for _, t in buttons} == {a.order_number, b.order_number}


async def test_stale_picker_button_is_explained_not_filed(replay_http, replay_session, monkeypatch):
    """TEST 8: the screenshot went to ORD-B; a later tap on ORD-A's button answers 'Your last screenshot is linked to ORD-B. Send a separate one for ORD-A'."""
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("777"))
    client, product, user, phone, pnid = await _seed(replay_session, "1623", stock=50)
    conv, a = await _pending_order(replay_session, client, product, phone, qty=1)
    b = await _second_order(replay_session, client, product, conv.id, phone, 2)
    a_id, a_no, b_id, b_no = a.id, a.order_number, b.id, b.order_number
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.stale.{time.time_ns()}")
    _, buttons = _last_buttons()
    id_a = next(i for i, t in buttons if t == a_no)
    id_b = next(i for i, t in buttons if t == b_no)
    await send_button(replay_http, phone, id_b, b_no, phone_number_id=pnid, wamid=f"wamid.stale.b.{time.time_ns()}")
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == b_id)) == 1
    await send_button(replay_http, phone, id_a, a_no, phone_number_id=pnid, wamid=f"wamid.stale.a.{time.time_ns()}")
    assert sent["texts"][-1] == f"Your last screenshot is linked to {b_no}. Please send a separate screenshot for {a_no} 📸"
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == a_id) == []
    # the same order's own button again is only an acknowledgement
    await send_button(replay_http, phone, id_b, b_no, phone_number_id=pnid, wamid=f"wamid.stale.b2.{time.time_ns()}")
    assert sent["texts"][-1] == f"That screenshot is already linked to {b_no}."
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == b_id)) == 1


# ── 5: payment status ────────────────────────────────────────────────────────

async def test_confirm_my_payments_is_a_per_order_status_without_the_planner(replay_http, replay_session, monkeypatch):
    """TEST 9: one order not paid, one under review, one paid → three lines, no planner."""
    from app.models.order import Order

    sent = stub_media_and_capture_sends(monkeypatch)
    _no_planner(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "1631", stock=50)
    conv, unpaid = await _pending_order(replay_session, client, product, phone, qty=1)
    reviewed = await _second_order(replay_session, client, product, conv.id, phone, 2)
    paid = await _second_order(replay_session, client, product, conv.id, phone, 3)
    for order, status in ((reviewed, "payment_submitted"), (paid, "paid")):
        row = await _fresh(replay_session, Order, order.id)
        row.status = status
    await replay_session.commit()

    await send_message(replay_http, phone, "Please confirm my payments", phone_number_id=pnid, wamid=f"wamid.status.{time.time_ns()}")
    text = sent["texts"][-1]
    assert text.startswith("Here is where your payments stand:")
    assert f"{unpaid.order_number}" in text and "not paid yet" in text
    assert f"{reviewed.order_number}" in text and "waiting for verification" in text
    assert f"{paid.order_number}" in text and "payment confirmed ✅" in text


# ── 7: unsupported message types ─────────────────────────────────────────────

def _typed_payload(sender: str, msg_type: str, pnid: str, wamid: str):
    """A webhook payload for a message type the bot cannot read (document / video / location / unsupported)."""
    import json

    from tests.replay.helpers import _sign

    payload = {"object": "whatsapp_business_account", "entry": [{"id": "E", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "0", "phone_number_id": pnid},
        "contacts": [{"profile": {"name": "T"}, "wa_id": sender}],
        "messages": [{"from": sender, "id": wamid, "timestamp": str(int(time.time())), "type": msg_type}],
    }}]}]}
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}


async def test_unsupported_message_type_gets_one_polite_answer_per_window(replay_http, replay_session, monkeypatch):
    """TEST 13: 'unsupported' / 'document' → 'Sorry, I can't open that type of message …' once; a reaction gets nothing."""
    from app.routers import webhook

    webhook._unsupported_notified.clear()
    sent = stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "1641")
    for kind in ("unsupported", "document", "reaction"):
        body, headers = _typed_payload(phone, kind, pnid, f"wamid.{kind}.{time.time_ns()}")
        assert (await replay_http.post("/webhook", content=body, headers=headers)).status_code == 200
    notices = [t for t in sent["texts"] if "can't open that type of message" in t]
    assert notices == ["Sorry, I can't open that type of message. Please send text, a photo or a voice note."], sent["texts"]
