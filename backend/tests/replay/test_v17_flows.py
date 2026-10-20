"""
Replay (real webhook → real Postgres) for the v1.7 payment-image fixes of conv=60 (ORD-2026-0077 / 0078 / 0079):

  3. unknown image → the customer taps [Payment screenshot] → they get a reply (attach, or the order picker) — never ``skip_send``
  4. every inbound message is answered; a burst's extra image stays silent ONLY while the covering reply is ≤ 10 s old (``no_reply_sent`` otherwise)
  5. a black frame is "too dark to read" (asked to resend), never filed, never matched to an order by the digits in OCR noise
  6. no amount on the receipt → the newest unpaid order is attached when its instructions are recent and the others are old; [Change order]
  7. approving a payment tells the customer and logs ``notify_payment_approved order=… sent=true``
  8. the event loop is not held while an image is classified (OCR / vision / hashing off the loop)

OCR / vision / downloads are scripted; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_button, send_image, send_message, stub_media_and_capture_sends
from tests.replay.test_payment_proof_hardening import _last_buttons, _ocr, _receipt, _second_order
from tests.replay.test_payment_verification_replay import _all, _fresh, _pending_order, _seed

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

BLACK_FRAME = Path(__file__).resolve().parent.parent / "fixtures" / "ocr" / "black_frame_receipt.jpg"


async def _backdate(session, order_id: int, hours: float) -> None:
    """Make an order (and so its payment instructions) `hours` old."""
    from app.models.order import Order

    order = await _fresh(session, Order, order_id)
    order.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    await session.commit()


async def _two_orders(session, client, product, phone, *, older_hours: float = 0.0):
    """The conversation's conv + two unpaid UPI orders (different totals); the older one can be aged."""
    conv, first = await _pending_order(session, client, product, phone, qty=2)
    second = await _second_order(session, client, product, conv.id, phone, qty=3)
    if older_hours:
        await _backdate(session, first.id, older_hours)
    return conv, first, second


def _no_planner(monkeypatch):
    """The planner must not be consulted on these paths."""
    async def _boom(*_a, **_k):
        """Fail the test if the planner is called."""
        raise AssertionError("the planner was called")

    monkeypatch.setattr("app.services.agent_planner.call_model", _boom)


# ── 3. the tap after an unknown image ────────────────────────────────────────

async def test_payment_screenshot_tap_is_answered_with_the_order_picker(replay_http, replay_session, monkeypatch, caplog):
    """
    conv=60: unknown image → [Payment screenshot | Product photo] → tap → ``Image burst … no reply of its own`` → skip_send. The tap is a customer
    answer, not a burst image: with two equally recent unpaid orders and no readable amount it must get the "Which order?" question.
    """
    from app.services import whatsapp_service

    caplog.set_level(logging.INFO)
    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "1701")
    conv, first, second = await _two_orders(replay_session, client, product, phone)

    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.a.{time.time_ns()}")
    assert whatsapp_service._raw_send_button_message.await_count == 1
    _, buttons = _last_buttons()
    pay_id = next(i for i, _ in buttons if i.startswith("v3_proof_payment"))

    caplog.clear()
    resp = await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    assert whatsapp_service._raw_send_button_message.await_count == 2, "the tap got no reply (the burst check swallowed it)"
    body, picks = _last_buttons()
    assert "which order" in body.lower() and len(picks) == 2
    assert not [r for r in caplog.records if "Image burst" in r.getMessage()], "the tap was treated as a burst image"


async def test_a_text_or_tap_is_never_part_of_an_image_burst(replay_http, replay_session, monkeypatch):
    """``_claim_image_reply`` only ever suppresses IMAGE messages."""
    from types import SimpleNamespace

    from app.services import payment_inbound as pi

    conv = SimpleNamespace(id=1, image_batch_reply_at=datetime.now(timezone.utc), ai_enabled=True)
    for kind in ("text", "interactive"):
        ctx = SimpleNamespace(conv=conv, message=SimpleNamespace(type=kind), db=None)
        assert await pi._claim_image_reply(ctx) is True


# ── 6. recency auto-attach ───────────────────────────────────────────────────

async def test_no_amount_attaches_to_the_newest_order_and_offers_change(replay_http, replay_session, monkeypatch):
    """ORD-0079-like (minutes old) vs an old unpaid order (12 h): the screenshot goes to the newest; [Change order] moves it."""
    from app.models.order import Order
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "1702")
    conv, old, new = await _two_orders(replay_session, client, product, phone, older_hours=12)

    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.b.{time.time_ns()}")
    _, buttons = _last_buttons()
    pay_id = next(i for i, _ in buttons if i.startswith("v3_proof_payment"))
    await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid)

    proofs = await _all(replay_session, PaymentProof, PaymentProof.order_id == new.id)
    assert len(proofs) == 1 and proofs[0].status == "pending", "the newest order got the screenshot"
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == old.id) == []
    assert (await _fresh(replay_session, Order, new.id)).status == "payment_submitted"
    body, change = _last_buttons()
    assert f"#{new.order_number}" in body and "wrong order" in body.lower()
    assert [t for _, t in change] == ["Change order"]

    # Wrong order? → the proof is withdrawn, the customer picks, the picture is filed under the other order
    change_id = change[0][0]
    await send_button(replay_http, phone, change_id, "Change order", phone_number_id=pnid)
    assert (await _fresh(replay_session, Order, new.id)).status == "pending_payment"
    body, picks = _last_buttons()
    assert "which order" in body.lower()
    pick_old = next(i for i, t in picks if t == old.order_number)
    await send_button(replay_http, phone, pick_old, old.order_number, phone_number_id=pnid)
    assert (await _fresh(replay_session, Order, old.id)).status == "payment_submitted"
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == old.id, PaymentProof.status == "pending")) == 1


async def test_with_two_recent_orders_the_customer_is_asked(replay_http, replay_session, monkeypatch):
    """Both orders were created minutes ago → no guess: the picker."""
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "1703")
    conv, first, second = await _two_orders(replay_session, client, product, phone, older_hours=2)
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.c.{time.time_ns()}")
    _, buttons = _last_buttons()
    pay_id = next(i for i, _ in buttons if i.startswith("v3_proof_payment"))
    await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid)
    body, picks = _last_buttons()
    assert "which order" in body.lower()
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id.in_([first.id, second.id])) == []


async def test_a_receipt_amount_beats_recency(replay_http, replay_session, monkeypatch):
    """The OCR amount matches the OLD order's total → it is filed there even though the new order is newer."""
    from app.models.payment_proof import PaymentProof

    stub_media_and_capture_sends(monkeypatch)
    client, product, user, phone, pnid = await _seed(replay_session, "1704")
    conv, old, new = await _two_orders(replay_session, client, product, phone, older_hours=12)
    _ocr(monkeypatch, _receipt(f"{int(old.total_amount):,}"))
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.d.{time.time_ns()}")
    assert len(await _all(replay_session, PaymentProof, PaymentProof.order_id == old.id)) == 1
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id == new.id) == []


# ── 5. the black frame ───────────────────────────────────────────────────────

async def test_a_black_frame_is_asked_again_and_never_filed(replay_http, replay_session, monkeypatch):
    """The conv=60 image (brightest pixel 6/255): "too dark to read — please send it again". No proof, no order picker, no digits-as-amount."""
    from app.models.payment_proof import PaymentProof

    sent = stub_media_and_capture_sends(monkeypatch)
    black = BLACK_FRAME.read_bytes()

    async def _download(media_id, *a, **kw):
        """The stored black frame."""
        return black

    monkeypatch.setattr("app.services.vision_service.download_whatsapp_media", _download)
    # real (unscripted) OCR noise must not matter: the classifier never reaches OCR for a blank frame
    client, product, user, phone, pnid = await _seed(replay_session, "1705")
    conv, first, second = await _two_orders(replay_session, client, product, phone)
    resp = await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.e.{time.time_ns()}")
    assert resp.status_code == 200, resp.text
    assert sent["texts"] and "too dark" in sent["texts"][-1], sent["texts"]
    assert await _all(replay_session, PaymentProof, PaymentProof.order_id.in_([first.id, second.id])) == []


# ── 4. every inbound message gets a reply ────────────────────────────────────

async def test_every_message_of_the_payment_flow_gets_a_reply(replay_http, replay_session, monkeypatch):
    """image → tap → 'paid' → 'paid' again: four inbound messages, four outbound replies (the second 'paid' used to be silent)."""
    from app.services import whatsapp_service

    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "1706")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)

    def outbound() -> int:
        """Messages the bot has sent so far."""
        return len(sent["texts"]) + whatsapp_service._raw_send_button_message.await_count

    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.f.{time.time_ns()}")
    assert outbound() == 1
    _, buttons = _last_buttons()
    pay_id = next(i for i, _ in buttons if i.startswith("v3_proof_payment"))
    await send_button(replay_http, phone, pay_id, "Payment screenshot", phone_number_id=pnid)
    assert outbound() == 2
    await send_message(replay_http, phone, "paid", phone_number_id=pnid, wamid=f"wamid.v17.g.{time.time_ns()}")
    assert outbound() == 3
    await send_message(replay_http, phone, "paid", phone_number_id=pnid, wamid=f"wamid.v17.h.{time.time_ns()}")
    assert outbound() == 4, "a typed message under review is answered every time"


async def test_a_burst_image_is_silent_only_while_a_reply_is_ten_seconds_old(replay_http, replay_session, monkeypatch, caplog):
    """Dedupe has a time bound: with the covering reply older than the window the extra image is answered and ``no_reply_sent`` is logged."""
    from app.services import order_pipeline as op

    caplog.set_level(logging.INFO)
    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, None)
    client, product, user, phone, pnid = await _seed(replay_session, "1707")
    await _pending_order(replay_session, client, product, phone, qty=2)

    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.i.{time.time_ns()}")
    asked = (await _count_buttons())
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.j.{time.time_ns()}")
    assert await _count_buttons() == asked, "within 10 s the second image of the burst is covered by the first reply"
    assert any("REPLY_DEDUPED" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(op, "NO_REPLY_DEDUPE_SECONDS", -1)         # the covering reply is "too old"
    before = len(sent["texts"]) + await _count_buttons()
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.k.{time.time_ns()}")
    assert len(sent["texts"]) + await _count_buttons() == before + 1, "no covering reply → the image is answered"
    assert any("no_reply_sent" in r.getMessage() for r in caplog.records)


async def _count_buttons() -> int:
    """How many interactive-button messages were sent."""
    from app.services import whatsapp_service

    return whatsapp_service._raw_send_button_message.await_count


# ── 7. approval reaches the customer ─────────────────────────────────────────

async def test_approving_a_payment_notifies_the_customer_and_logs_it(replay_http, replay_session, monkeypatch, caplog):
    """``notify_payment_approved order=… sent=true`` + the customer receives "Payment confirmed"."""
    from app.models.client import Client
    from app.services import payment_verification_service as pvs

    caplog.set_level(logging.INFO)
    sent = stub_media_and_capture_sends(monkeypatch)
    _ocr(monkeypatch, _receipt("1,300"))
    client, product, user, phone, pnid = await _seed(replay_session, "1708")
    conv, order = await _pending_order(replay_session, client, product, phone, qty=2)
    await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.l.{time.time_ns()}")
    number = order.order_number
    live_client = await replay_session.get(Client, client.id)
    result = await pvs.approve(replay_session, live_client, order.id, user, confirm_high_value=True)
    assert result.customer_notified is True
    assert any("Payment confirmed" in t and number in t for t in sent["texts"]), sent["texts"]
    assert any(f"notify_payment_approved order={number} sent=true" in r.getMessage() for r in caplog.records)


# ── 8. the loop is free while an image is classified ─────────────────────────

async def test_the_event_loop_is_not_held_while_an_image_is_classified(replay_http, replay_session, monkeypatch):
    """OCR, vision and hashing take ~0.3 s each here — all off the loop: a 10 ms timer never fires more than 50 ms late."""
    from app.services import payment_proof_classifier as clf

    from app.main import prewarm_imports

    prewarm_imports()                  # the app does this at startup (lifespan); the first turn must not pay for lazy imports
    stub_media_and_capture_sends(monkeypatch)

    def slow_ocr(_bytes):
        """A synchronous OCR (like Tesseract through subprocess) that takes 300 ms — it runs in the worker thread."""
        time.sleep(0.3)
        return "PhonePe\nPayment successful\n₹ 1,300\nUTR 412345678901\nTransaction ID T2610201234567890123"

    monkeypatch.setattr(clf, "_ocr_sync", slow_ocr)

    async def real_ocr_text(image_bytes):
        """The production wrapper (the conftest does not replace it here)."""
        return await asyncio.to_thread(clf._ocr_sync, image_bytes)

    monkeypatch.setattr(clf, "ocr_text", real_ocr_text)
    client, product, user, phone, pnid = await _seed(replay_session, "1709")
    await _pending_order(replay_session, client, product, phone, qty=2)
    lags: list[float] = []

    async def probe():
        """How late does a 10 ms timer fire?"""
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    watcher = asyncio.create_task(probe())
    resp = await send_image(replay_http, phone, phone_number_id=pnid, wamid=f"wamid.v17.m.{time.time_ns()}")
    watcher.cancel()
    assert resp.status_code == 200, resp.text
    assert max(lags) < 0.05, f"the event loop was held for {max(lags):.3f}s during image classification"
