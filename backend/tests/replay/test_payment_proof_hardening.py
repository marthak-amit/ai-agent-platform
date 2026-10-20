"""
Replay (real webhook → real Postgres) for the conv=60 payment-proof fixes:

  * a product photo sent while an order awaits payment is NOT filed as a payment proof;
  * an image the classifier cannot place is asked about ([Payment screenshot] [Product photo]) and the tap decides;
  * "This is product not the payment" withdraws the last proof (audit row kept, gone from the pending queue, order back to
    pending_payment) and is never answered with the payment instruction again;
  * a real UPI screenshot is filed under the right order when the customer has two unpaid ones (amount match) — and the customer is
    asked which one when the amount does not decide it;
  * the dashboard's approve of a high-value order answers 409 ``high_value_confirmation_required`` (no 500) and works with the flag.

The OCR is scripted per test (``payment_proof_classifier.ocr_text``); no vision model and no network are used.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_button, send_image, send_message, stub_media_and_capture_sends
from tests.replay.test_payment_verification_replay import _all, _fresh, _pending_order, _seed, seller  # noqa: F401  (seller is a fixture)

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


def _ocr(monkeypatch, text):
    """Script the whole-image OCR (None = unavailable)."""
    async def _fake(_bytes):
        """Scripted OCR."""
        return text

    monkeypatch.setattr("app.services.payment_proof_classifier.ocr_text", _fake)


def _receipt(amount: str) -> str:
    """OCR text of a UPI receipt for `amount` (e.g. '1,300')."""
    return f"PhonePe\nPayment successful\nPaid to Proof Store\n₹{amount}\nTransaction ID T2610201234567890123\nUTR 412345678901"


def _last_buttons():
    """(body, [(id, title)]) of the most recent interactive-button message the bot sent."""
    from app.services import whatsapp_service

    call = whatsapp_service._raw_send_button_message.call_args_list[-1]
    return call.kwargs["body_text"], [(b["id"], b["title"]) for b in call.kwargs["buttons"]]


async def _second_order(session, client, product, conv_id, phone, qty):
    """Another unpaid UPI order of the same conversation (a different total)."""
    from app.services import order_service

    return await order_service.create_order(
        db=session, client_id=client.id, customer_name="Asha Shah", customer_phone=phone, delivery_address="12 MG Road, Surat",
        product_name=product.name, quantity=qty, unit_price=product.price, payment_method="UPI", product_id=product.id,
        product_sku=product.sku, conversation_id=conv_id,
    )


# ── 5. a product photo is not a payment proof ────────────────────────────────

async def test_product_photo_in_payment_stage_is_not_attached_as_proof(replay_http, replay_session, monkeypatch):
    """The conv=60 case: a photo with no receipt text → no PaymentProof row, the order stays pending_payment, no 'verify your payment' ack."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, "")
    client, product, user, phone, pnid = await _seed(replay_session, "0101")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    oid = order.id

    resp = await send_image(replay_http, phone, phone_number_id=pnid)
    assert resp.status_code == 200, resp.text

    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == oid) == []
    assert (await _fresh(replay_session, Order, oid)).status == "pending_payment"
    assert not any("verify your payment" in t or "Our team will verify" in t for t in sent["texts"]), sent["texts"]
    assert (await replay_http.get("/payments/pending/count")).status_code in (200, 401, 403)


# ── unsure → ask with buttons ────────────────────────────────────────────────

async def test_unsure_image_is_asked_about_and_the_tap_decides(replay_http, replay_session, monkeypatch):
    """OCR unavailable and no vision: [Payment screenshot] [Product photo]; 'Payment screenshot' files it, 'Product photo' does not."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "0102")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    oid = order.id

    await send_image(replay_http, phone, phone_number_id=pnid, wamid="wamid.unsure.1")
    body, buttons = _last_buttons()
    assert "payment screenshot" in body.lower() and "product photo" in body.lower()
    assert [t for _, t in buttons] == ["Payment screenshot", "Product photo"]
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == oid) == [], "nothing is filed before the customer answers"

    pay_id = next(i for i, _ in buttons if i.startswith("v3_proof_payment"))
    resp = await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)
    assert len(proofs) == 1 and proofs[0].status == "pending"
    assert (await _fresh(replay_session, Order, oid)).status == "payment_submitted"
    assert sent["texts"][-1].startswith("Thanks!")

    # the same tap again (WhatsApp buttons stay tappable) files nothing twice
    await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid, wamid="wamid.unsure.tap2")
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)) == 1


async def test_tapping_product_photo_files_nothing(replay_http, replay_session, monkeypatch):
    """'Product photo' → the invitation to share a code / say what they look for; no proof."""
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "0103")
    conv, order = await _pending_order(replay_session, client, product, phone)
    await send_image(replay_http, phone, phone_number_id=pnid, wamid="wamid.unsure.2")
    _, buttons = _last_buttons()
    prod_id = next(i for i, _ in buttons if i.startswith("v3_proof_product"))
    await send_button(replay_http, phone, prod_id, "Product photo", phone_number_id=pnid)
    assert "find something similar" in sent["texts"][-1]
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == order.id) == []


# ── 6. "this is product not the payment" ─────────────────────────────────────

async def test_denial_withdraws_the_proof_and_never_resends_the_payment_instruction(replay_http, replay_session, seller, monkeypatch):
    """image filed as proof → 'This is product not the payment' → proof withdrawn (row + audit kept), order pending again, pending queue empty,
    the reply moves to the product flow; a second 'No this is not payment' is answered plainly — never with the UPI instruction."""
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("1,300"))        # the first image reads as a receipt (the conv=60 misfire, reproduced on purpose)
    client, product, user, phone, pnid = await _seed(replay_session, "0104")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    seller(user)
    oid = order.id

    await send_image(replay_http, phone, phone_number_id=pnid)
    assert (await _fresh(replay_session, Order, oid)).status == "payment_submitted"
    assert (await replay_http.get("/payments/pending/count")).json() == {"count": 1}

    resp = await send_message(replay_http, phone, "This is product not the payment", phone_number_id=pnid, wamid=f"wamid.den.1.{time.time_ns()}")
    assert resp.status_code == 200, resp.text
    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)
    assert [p.status for p in proofs] == ["withdrawn"] and proofs[0].reviewed_by_name == "customer"
    order_row = await _fresh(replay_session, Order, oid)
    assert order_row.status == "pending_payment" and order_row.payment_submitted_at is None
    audit = await _all(replay_session, OrderAuditLog, OrderAuditLog.order_id == oid, order=OrderAuditLog.id)
    assert [a.action for a in audit] == ["payment_submitted", "proof_withdrawn"]
    assert (await replay_http.get("/payments/pending")).json() == []
    assert (await replay_http.get("/payments/pending/count")).json() == {"count": 0}
    reply = sent["texts"][-1]
    assert "taken that image out" in reply and "find something similar" in reply
    assert "Pay to UPI ID" not in reply

    n_before = len(sent["texts"])
    await send_message(replay_http, phone, "No this is not payment", phone_number_id=pnid, wamid=f"wamid.den.2.{time.time_ns()}")
    second = sent["texts"][n_before:]
    assert second and all("Pay to UPI ID" not in t and "UPI ID" not in t for t in second), second
    assert "no payment screenshot is attached" in second[-1]
    assert [p.status for p in await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)] == ["withdrawn"]


# ── 7. two unpaid orders: the amount decides ─────────────────────────────────

async def test_real_screenshot_goes_to_the_order_whose_total_is_on_it(replay_http, replay_session, monkeypatch):
    """Two unpaid orders (₹1,300 and ₹650): a receipt for ₹650 is filed under the ₹650 order only, and the ack names it."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("650"))
    client, product, user, phone, pnid = await _seed(replay_session, "0105")
    conv, big = await _pending_order(replay_session, client, product, phone, qty=2)
    small = await _second_order(replay_session, client, product, conv.id, phone, 1)
    big_id, small_id, small_no = big.id, small.id, small.order_number

    await send_image(replay_http, phone, phone_number_id=pnid)

    assert (await _fresh(replay_session, Order, small_id)).status == "payment_submitted"
    assert (await _fresh(replay_session, Order, big_id)).status == "pending_payment"
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == small_id)) == 1
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == big_id) == []
    assert small_no in sent["texts"][-1]


async def test_ambiguous_screenshot_asks_which_order(replay_http, replay_session, monkeypatch):
    """The receipt's amount matches neither order → [order] [order] buttons; the tap files it under the chosen one."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("777"))
    client, product, user, phone, pnid = await _seed(replay_session, "0106")
    conv, big = await _pending_order(replay_session, client, product, phone, qty=2)
    small = await _second_order(replay_session, client, product, conv.id, phone, 1)
    big_id, big_no = big.id, big.order_number

    await send_image(replay_http, phone, phone_number_id=pnid)
    body, buttons = _last_buttons()
    assert "Which order" in body and {t for _, t in buttons} == {big_no, small.order_number}
    assert await _all(replay_session, PaymentProof, PaymentProof.client_id == client.id) == [], "never filed under a guess"

    chosen = next(i for i, t in buttons if t == big_no)
    await send_button(replay_http, phone, chosen, big_no, phone_number_id=pnid)
    assert (await _fresh(replay_session, Order, big_id)).status == "payment_submitted"
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == big_id)) == 1


# ── 8. approve a high-value order over HTTP ──────────────────────────────────

async def test_approving_a_high_value_order_answers_409_with_the_payload_then_200(replay_http, replay_session, seller, monkeypatch):
    """No 500 (MissingGreenlet): 409 {code: high_value_confirmation_required, order_number, total, threshold, message}; the retry with the flag is 200."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "0107")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    oid, number = order.id, order.order_number
    row = await _fresh(replay_session, Order, oid)
    row.total_amount = 150000.0
    await replay_session.commit()
    seller(user)

    await send_image(replay_http, phone, phone_number_id=pnid)
    assert (await _fresh(replay_session, Order, oid)).status == "payment_submitted"

    res = await replay_http.post(f"/orders/{oid}/payment/approve")
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "high_value_confirmation_required" and detail["order_number"] == number
    assert detail["total"] == 150000.0 and detail["threshold"] == 50000.0 and number in detail["message"] and "bank" in detail["message"]
    assert (await _fresh(replay_session, Order, oid)).status == "payment_submitted"          # nothing changed

    ok = await replay_http.post(f"/orders/{oid}/payment/approve", json={"confirm_high_value": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "paid"
    assert [p.status for p in await _all(replay_session, PaymentProof, PaymentProof.order_id == oid)] == ["approved"]
