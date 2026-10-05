"""
Unit tests for the manual-UPI verification + dashboard outbound feature.

DB-free: pure helpers are called directly; the service-level state changes run
against an in-memory order with the persistence layer stubbed, so they assert
the decision logic (transition guards, idempotency, who gets messaged) rather
than SQL. The Postgres-backed end-state assertions live in
tests/replay/test_payment_verification_replay.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import (
    channel_sender,
    conversation_control,
    media_service,
    payment_inbound,
    realtime_service,
)
from app.services import payment_verification_service as pvs
from app.services.language_templates import get_template
from app.services.order_state_machine import (
    InvalidOrderTransition,
    assert_order_transition,
    can_transition_order,
)


# ─── helpers ──────────────────────────────────────────────────────────────────


def _order(status="payment_submitted", **kw):
    """Minimal in-memory order."""
    base = dict(
        id=7, client_id=1, order_number="ORD-2026-0007", status=status, total_amount=1299.0,
        stock_deducted=False, payment_status="pending", paid_at=None, confirmed_at=None,
        conversation_id=3, line_items=[], product_name="Kurta", quantity=1,
        variant_color=None, variant_size=None, payment_submitted_at=None,
        cancelled_at=None, cancel_reason=None, payment_method="UPI",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _client(**kw):
    """Minimal in-memory client."""
    base = dict(
        id=1, upi_id="shop@okaxis", upi_display_name="Riya Sarees", upi_qr_url=None,
        payment_instructions=None, phone="919999999999", whatsapp_phone_number_id="pn",
        whatsapp_access_token="tok", instagram_account_id=None, payment_setup_alert_at=None,
        bot_auto_resume_minutes=30, payment_expiry_hours=24,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _conv(**kw):
    """Minimal in-memory conversation."""
    base = dict(
        id=3, channel="whatsapp", phone_number="919876543210", ai_enabled=True,
        last_customer_language="english", current_stage="payment", is_sandbox=False,
        last_proof_ack_at=None, bot_paused_at=None, bot_pause_source=None,
        human_last_activity_at=None, taken_over_at=None, taken_over_note=None,
        slot_attempt_count=0, slot_attempt_slot=None, off_topic_count=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _db():
    """AsyncSession stand-in."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def stubbed_service(monkeypatch):
    """Stub the persistence/IO edges of payment_verification_service; return the call log."""
    calls = SimpleNamespace(
        deducted=0, notified=[], side_effects=0, released=0, order=None, audits=[],
    )

    async def lock(db, client_id, order_id):
        """Return the shared in-memory order."""
        return calls.order

    async def pending(db, order_id):
        """No pending proof rows in the stubbed layer."""
        return []

    async def deduct(db, order):
        """Count a stock deduction."""
        calls.deducted += 1
        order.stock_deducted = True

    async def conv_for(db, order):
        """Return a conversation."""
        return _conv()

    async def notify(db, client, conv, key, **kw):
        """Record the template sent."""
        calls.notified.append(key)
        return channel_sender.SendOutcome(True)

    async def side(db, order, client):
        """Count post-paid side effects."""
        calls.side_effects += 1

    async def release(db, order):
        """Count a reservation release."""
        calls.released += 1

    async def reset(db, conv):
        """No-op conversation reset."""

    monkeypatch.setattr(pvs, "_lock_order", lock)
    monkeypatch.setattr(pvs, "_pending_proofs", pending)
    monkeypatch.setattr(pvs.order_service, "apply_stock_deduction", deduct)
    monkeypatch.setattr(pvs, "_conversation_for", conv_for)
    monkeypatch.setattr(pvs, "_notify_customer", notify)
    monkeypatch.setattr(pvs.order_service, "run_post_paid_side_effects", side)
    monkeypatch.setattr(pvs.stock_reservation_service, "release_for_order", release)
    monkeypatch.setattr(pvs, "_reset_conversation_after_terminal", reset)
    monkeypatch.setattr(pvs, "_audit", lambda db, order, **kw: calls.audits.append(kw["action"]))
    return calls


# ─── state machine ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("current,target", [
    ("pending_payment", "payment_submitted"),
    ("payment_submitted", "paid"),
    ("payment_submitted", "pending_payment"),
    ("pending_payment", "cancelled"),
    ("payment_submitted", "cancelled"),
])
def test_can_transition_order_allows_spec_edges(current, target):
    """Every edge in the spec is permitted."""
    assert can_transition_order(current, target)


@pytest.mark.parametrize("current,target", [
    ("pending_payment", "paid"),      # no proof → can't approve
    ("paid", "cancelled"),
    ("paid", "pending_payment"),
    ("cancelled", "pending_payment"),
    ("cancelled", "paid"),
])
def test_can_transition_order_blocks_other_edges(current, target):
    """Anything outside the table is refused."""
    assert not can_transition_order(current, target)


def test_assert_order_transition_raises_with_edge():
    """assert_order_transition reports the offending edge."""
    with pytest.raises(InvalidOrderTransition) as exc:
        assert_order_transition("pending_payment", "paid")
    assert (exc.value.current, exc.value.target) == ("pending_payment", "paid")
    assert_order_transition("payment_submitted", "paid")  # allowed edge: no raise


# ─── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("upi,ok", [
    ("shop@okaxis", True), ("riya.sarees-1@ybl", True), ("9876543210@paytm", True),
    ("noatsign", False), ("a@b", False), ("@ybl", False), ("shop@", False), ("", False),
    ("two@@ybl", False), ("sp ace@ybl", False),
])
def test_is_valid_upi_id(upi, ok):
    """name@handle validation."""
    assert pvs.is_valid_upi_id(upi) is ok


def test_build_upi_uri_matches_spec_format():
    """The QR payload follows upi://pay?pa=&pn=&am=&cu=INR&tn=Order{id}."""
    uri = pvs.build_upi_uri("shop@okaxis", "Riya Sarees", 1299, 42)
    assert uri == "upi://pay?pa=shop@okaxis&pn=Riya%20Sarees&am=1299.00&cu=INR&tn=Order42"


def test_make_qr_png_returns_png_bytes():
    """The generated QR is a real PNG."""
    png = pvs.make_qr_png(pvs.build_upi_uri("a@b.c", None, 10, 1))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("text", [
    "paid", "Paid!", "done", "payment kar diya", "Payment ho gaya", "I have paid",
    "i've paid", "kar diya", "pay kar diya", "पेमेंट कर दिया", "ચુકવણી કરી દીધી",
])
def test_is_paid_intent_true(text):
    """Short "I've paid" messages in EN/HI/GU count as a paid claim."""
    assert pvs.is_paid_intent(text)


@pytest.mark.parametrize("text", [
    "", "hello", "what is the price", "is my order done being packed and when will it be delivered to me",
    "send me the catalogue",
])
def test_is_paid_intent_false(text):
    """Unrelated or long sentences are not payment claims."""
    assert not pvs.is_paid_intent(text)


def test_actor_label_prefers_email_then_system():
    """Audit label falls back to 'system'."""
    assert pvs.actor_label(SimpleNamespace(email="a@b.com")) == "a@b.com"
    assert pvs.actor_label(None) == "system"


def test_render_items_single_and_cart():
    """Items render from line items, or from the flat columns for legacy orders."""
    legacy = _order(product_name="Kurta", quantity=2, total_amount=1000.0, variant_color="Red", variant_size="M")
    assert "Kurta (Red/M) × 2" in pvs.render_items(legacy)
    li = SimpleNamespace(product_name="Saree", variant_color="Blue", variant_size=None,
                         variant_material=None, quantity=3, subtotal=2400.0)
    cart = _order(line_items=[li])
    assert pvs.render_items(cart) == "• Saree (Blue) × 3 = ₹2,400"


@pytest.mark.parametrize("lang,needle", [
    ("english", "Pay to UPI ID"), ("hindi_roman", "UPI ID par"), ("hindi_devanagari", "UPI ID पर"),
    ("gujarati_roman", "UPI ID par payment karo"), ("gujarati_script", "UPI ID પર"),
])
def test_instruction_text_localised_with_amount_and_upi(lang, needle):
    """Instruction text carries order #, amount and UPI ID in every language."""
    text = pvs.instruction_text(_order(), _client(), lang)
    assert needle in text
    assert "ORD-2026-0007" in text and "₹1,299" in text and "shop@okaxis" in text
    assert "Riya Sarees" in text


async def test_build_payment_instruction_includes_qr_and_closing(monkeypatch):
    """QR image is generated + uploaded and the closing line rides as its caption."""
    stored = {}

    async def fake_store(client_id, data, ctype, folder="chat"):
        """Capture what would be uploaded."""
        stored.update(client_id=client_id, ctype=ctype, folder=folder, png=data[:4])
        return "https://cdn.test/qr.png"

    monkeypatch.setattr(pvs.media_service, "store_media", fake_store)
    instr = await pvs.build_payment_instruction(_db(), _order(), _client(), _conv())
    assert instr.qr_url == "https://cdn.test/qr.png"
    assert instr.closing == "After payment, please send the payment screenshot here."
    assert instr.closing not in instr.text
    assert stored["ctype"] == "image/png" and stored["png"] == b"\x89PNG"


async def test_build_payment_instruction_without_qr_folds_in_closing(monkeypatch):
    """No QR possible and no static QR → the closing line is appended to the text."""
    async def boom(*a, **k):
        """Upload failure."""
        raise RuntimeError("r2 down")

    monkeypatch.setattr(pvs.media_service, "store_media", boom)
    instr = await pvs.build_payment_instruction(_db(), _order(), _client(), _conv())
    assert instr.qr_url is None
    assert instr.text.endswith("After payment, please send the payment screenshot here.")


async def test_build_payment_instruction_falls_back_to_static_qr(monkeypatch):
    """A generation failure falls back to the seller's uploaded static QR."""
    async def boom(*a, **k):
        """Upload failure."""
        raise RuntimeError("r2 down")

    monkeypatch.setattr(pvs.media_service, "store_media", boom)
    instr = await pvs.build_payment_instruction(
        _db(), _order(), _client(upi_qr_url="https://cdn.test/static.png"), _conv())
    assert instr.qr_url == "https://cdn.test/static.png"


def test_proof_ack_due_throttles_to_ten_minutes():
    """The 'verification in progress' ack is allowed once per 10 minutes."""
    now = datetime.now(timezone.utc)
    assert pvs.proof_ack_due(_conv(last_proof_ack_at=None))
    assert not pvs.proof_ack_due(_conv(last_proof_ack_at=now - timedelta(minutes=9)))
    assert pvs.proof_ack_due(_conv(last_proof_ack_at=now - timedelta(minutes=10, seconds=1)))
    naive = (now - timedelta(minutes=1)).replace(tzinfo=None)
    assert not pvs.proof_ack_due(_conv(last_proof_ack_at=naive))


# ─── approve / reject / cancel decisions ──────────────────────────────────────


async def test_approve_pays_deducts_once_and_notifies_once(stubbed_service):
    """First approve flips to paid, deducts stock once and sends the template once."""
    stubbed_service.order = _order("payment_submitted")
    user = SimpleNamespace(id=9, email="owner@shop.com")
    res = await pvs.approve(_db(), _client(), 7, user)
    assert res.changed and res.order.status == "paid"
    assert res.order.payment_status == "paid" and res.order.paid_at is not None
    assert stubbed_service.deducted == 1
    assert stubbed_service.notified == ["pay_confirmed"]
    assert stubbed_service.side_effects == 1
    assert stubbed_service.audits == ["payment_approved"]
    assert res.customer_notified is True


async def test_approve_twice_is_idempotent(stubbed_service):
    """A double-click approve never double-deducts or double-sends."""
    stubbed_service.order = _order("payment_submitted")
    user = SimpleNamespace(id=9, email="owner@shop.com")
    first = await pvs.approve(_db(), _client(), 7, user)
    second = await pvs.approve(_db(), _client(), 7, user)
    assert first.changed and not second.changed
    assert stubbed_service.deducted == 1
    assert stubbed_service.notified == ["pay_confirmed"]
    assert stubbed_service.side_effects == 1


async def test_approve_without_proof_is_invalid_transition(stubbed_service):
    """pending_payment (no screenshot yet) cannot be approved."""
    stubbed_service.order = _order("pending_payment")
    with pytest.raises(InvalidOrderTransition):
        await pvs.approve(_db(), _client(), 7, None)
    assert stubbed_service.deducted == 0 and stubbed_service.notified == []


async def test_approve_cancelled_order_is_invalid(stubbed_service):
    """A cancelled order can't be approved."""
    stubbed_service.order = _order("cancelled")
    with pytest.raises(InvalidOrderTransition):
        await pvs.approve(_db(), _client(), 7, None)


async def test_reject_returns_to_pending_with_reason_and_is_idempotent(stubbed_service):
    """Reject → pending_payment, template sent once; repeat is a no-op."""
    stubbed_service.order = _order("payment_submitted")
    user = SimpleNamespace(id=9, email="owner@shop.com")
    first = await pvs.reject(_db(), _client(), 7, user, "  Amount mismatch ")
    again = await pvs.reject(_db(), _client(), 7, user, "Amount mismatch")
    assert first.changed and first.order.status == "pending_payment"
    assert not again.changed
    assert stubbed_service.notified == ["pay_rejected"]
    assert stubbed_service.deducted == 0
    assert stubbed_service.audits == ["payment_rejected"]


async def test_reject_paid_order_is_invalid(stubbed_service):
    """Rejecting an already-paid order is refused."""
    stubbed_service.order = _order("paid")
    with pytest.raises(InvalidOrderTransition):
        await pvs.reject(_db(), _client(), 7, None)


async def test_cancel_releases_reservation_and_is_idempotent(stubbed_service):
    """Cancel releases stock, stamps reason, tells the customer once."""
    stubbed_service.order = _order("pending_payment")
    res = await pvs.cancel(_db(), _client(), 7, SimpleNamespace(id=9, email="o@s.com"), "Out of stock")
    again = await pvs.cancel(_db(), _client(), 7, None, "Out of stock")
    assert res.changed and res.order.status == "cancelled"
    assert res.order.cancel_reason == "Out of stock" and res.order.cancelled_at is not None
    assert not again.changed
    assert stubbed_service.released == 1
    assert stubbed_service.notified == ["pay_cancelled"]
    assert stubbed_service.deducted == 0


async def test_cancel_paid_order_is_invalid(stubbed_service):
    """A paid order can't be cancelled through the payment flow."""
    stubbed_service.order = _order("paid")
    with pytest.raises(InvalidOrderTransition):
        await pvs.cancel(_db(), _client(), 7, None)


async def test_notify_customer_ignores_bot_pause(monkeypatch):
    """Approve/reject templates still send while the bot is paused."""
    sent = {}

    async def fake_send(db, client, conv, **kw):
        """Capture the send."""
        sent.update(kw)
        return channel_sender.SendOutcome(True)

    monkeypatch.setattr(pvs.channel_sender, "send_to_conversation", fake_send)
    out = await pvs._notify_customer(
        _db(), _client(), _conv(ai_enabled=False, bot_pause_source="human_send"),
        "pay_confirmed", order_number="ORD-1")
    assert out.sent and "Payment confirmed ✅ Your order #ORD-1 is confirmed." == sent["text"]
    assert sent["sender_type"] == "system"


async def test_notify_customer_skips_sandbox_and_missing_conversation():
    """No send for sandbox conversations or orders without one."""
    assert await pvs._notify_customer(_db(), _client(), None, "pay_confirmed", order_number="1") is None
    assert await pvs._notify_customer(_db(), _client(), _conv(is_sandbox=True), "pay_confirmed", order_number="1") is None


def test_reject_template_text_matches_spec():
    """Reject copy: 'We couldn't confirm your payment{: reason}. Please check…'."""
    with_reason = get_template("english", "pay_rejected", reason_part=": Amount mismatch")
    without = get_template("english", "pay_rejected", reason_part="")
    assert with_reason == "We couldn't confirm your payment: Amount mismatch. Please check and send the screenshot again."
    assert without == "We couldn't confirm your payment. Please check and send the screenshot again."


@pytest.mark.parametrize("key", [
    "pay_instruction", "pay_send_screenshot", "pay_proof_received", "pay_proof_more",
    "pay_ask_screenshot", "pay_confirmed", "pay_rejected", "pay_cancelled", "pay_unavailable",
])
@pytest.mark.parametrize("lang", [
    "english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script",
])
def test_payment_templates_exist_in_every_language(key, lang):
    """Every payment template is defined natively for EN/HI/GU (no silent English fallback)."""
    from app.services.language_templates import TEMPLATES

    assert key in TEMPLATES[lang]


# ─── conversation control (bot pause / auto-resume) ──────────────────────────


def test_auto_resume_only_for_human_send_pauses():
    """Manual and escalation pauses never auto-resume."""
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    client = _client()
    assert conversation_control.auto_resume_due(
        _conv(ai_enabled=False, bot_pause_source="human_send", human_last_activity_at=old), client)
    assert not conversation_control.auto_resume_due(
        _conv(ai_enabled=False, bot_pause_source="manual", human_last_activity_at=old), client)
    assert not conversation_control.auto_resume_due(
        _conv(ai_enabled=False, bot_pause_source=None, human_last_activity_at=old), client)
    assert not conversation_control.auto_resume_due(_conv(ai_enabled=True), client)


def test_auto_resume_respects_idle_minutes_and_zero_disables():
    """Resume fires after the configured idle time; 0 disables it."""
    now = datetime.now(timezone.utc)
    recent = _conv(ai_enabled=False, bot_pause_source="human_send", human_last_activity_at=now - timedelta(minutes=10))
    assert not conversation_control.auto_resume_due(recent, _client(bot_auto_resume_minutes=30))
    assert conversation_control.auto_resume_due(recent, _client(bot_auto_resume_minutes=5))
    assert conversation_control.auto_resume_at(recent, _client(bot_auto_resume_minutes=0)) is None


async def test_pause_bot_sets_flags_and_publishes():
    """pause_bot disables the AI, records source/activity and emits an event."""
    conv, client = _conv(), _client()
    queue = realtime_service.subscribe(client.id)
    try:
        await conversation_control.pause_bot(_db(), client, conv, source="human_send")
        assert conv.ai_enabled is False and conv.bot_pause_source == "human_send"
        assert conv.human_last_activity_at is not None
        event = queue.get_nowait()
        assert event == {"type": "conversation_updated", "data": {"conversation_id": 3, "bot_paused": True}}
    finally:
        realtime_service.unsubscribe(client.id, queue)


async def test_manual_pause_upgrades_an_auto_pause():
    """A manual toggle after an auto pause stops it from ever auto-resuming."""
    conv, client = _conv(), _client()
    await conversation_control.pause_bot(_db(), client, conv, source="human_send")
    await conversation_control.pause_bot(_db(), client, conv, source="manual")
    assert conv.bot_pause_source == "manual"


async def test_resume_bot_clears_state_and_counters():
    """resume_bot re-enables the AI and zeroes the escalation counters."""
    conv = _conv(ai_enabled=False, bot_pause_source="manual", slot_attempt_count=4, off_topic_count=2,
                 taken_over_at=datetime.now(timezone.utc))
    await conversation_control.resume_bot(_db(), _client(), conv)
    assert conv.ai_enabled is True and conv.bot_pause_source is None and conv.taken_over_at is None
    assert conv.slot_attempt_count == 0 and conv.off_topic_count == 0


async def test_maybe_auto_resume_resumes_idle_human_pause():
    """maybe_auto_resume flips an idled-out pause back on and reports it."""
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    conv = _conv(ai_enabled=False, bot_pause_source="human_send", human_last_activity_at=old)
    assert await conversation_control.maybe_auto_resume(_db(), _client(), conv) is True
    assert conv.ai_enabled is True
    assert await conversation_control.maybe_auto_resume(_db(), _client(), conv) is False


# ─── inbound hooks ────────────────────────────────────────────────────────────


def _ctx(msg_type="text", text="hello", conv=None, media_url=None):
    """Minimal InboundContext stand-in for payment_inbound."""
    return SimpleNamespace(
        db=_db(), client=_client(), conv=conv or _conv(),
        message=SimpleNamespace(type=msg_type, image=SimpleNamespace(id="m1") if msg_type == "image" else None),
        user_text=text, wamid="wamid.1", media_url=media_url,
        media_type="image" if media_url else None, download_media=AsyncMock(return_value=b"x"),
    )


@pytest.fixture
def inbound_stubs(monkeypatch):
    """Stub order lookup + persistence for payment_inbound; return the call log."""
    log = SimpleNamespace(order=None, saved=[], proofs=[], transitioned=True)

    async def find(db, conv_id):
        """Return the staged open order."""
        return log.order

    async def save(db, conv_id, role, content, **kw):
        """Record persisted messages."""
        log.saved.append((role, content, kw))
        return SimpleNamespace(id=len(log.saved))

    async def submit(db, client, conv, order_id, *, message_id, media_url):
        """Record a proof submission."""
        log.proofs.append(media_url)
        return pvs.ProofSubmission(proof=SimpleNamespace(id=1), transitioned=log.transitioned)

    monkeypatch.setattr(pvs, "find_open_payment_order", find)
    monkeypatch.setattr(pvs, "submit_proof", submit)
    monkeypatch.setattr(payment_inbound.conversation_service, "save_message", save)
    return log


async def test_image_while_pending_payment_creates_proof_and_acks(inbound_stubs):
    """Screenshot on a pending_payment order → proof + 'team will verify' reply."""
    inbound_stubs.order = _order("pending_payment")
    ctx = _ctx("image", "[image]", media_url="https://cdn.test/p.jpg")
    res = await payment_inbound._handle_payment_message(ctx)
    assert inbound_stubs.proofs == ["https://cdn.test/p.jpg"]
    assert res.text == "Thanks! Our team will verify your payment shortly and update you here."
    assert ctx.conv.last_proof_ack_at is not None
    assert [s[0] for s in inbound_stubs.saved] == ["user", "assistant"]
    assert inbound_stubs.saved[0][2]["media_url"] == "https://cdn.test/p.jpg"


async def test_extra_image_while_submitted_replies_once_per_ten_minutes(inbound_stubs):
    """Extra screenshots add proofs; the ack is throttled."""
    inbound_stubs.order = _order("payment_submitted")
    inbound_stubs.transitioned = False
    ctx = _ctx("image", "[image]", media_url="https://cdn.test/p2.jpg")
    first = await payment_inbound._handle_payment_message(ctx)
    second = await payment_inbound._handle_payment_message(ctx)
    assert first.text == "Received, verification in progress."
    assert second.skip_send is True
    assert len(inbound_stubs.proofs) == 2


async def test_paid_text_without_image_asks_for_screenshot(inbound_stubs):
    """'paid' with no image never creates a proof; it asks for the screenshot."""
    inbound_stubs.order = _order("pending_payment")
    res = await payment_inbound._handle_payment_message(_ctx("text", "payment kar diya"))
    assert res.text == "Please send the payment screenshot here so we can verify it. 📸"
    assert inbound_stubs.proofs == []


async def test_paid_text_when_already_submitted_gets_throttled_progress_reply(inbound_stubs):
    """'paid' after a screenshot is already under review → throttled progress ack."""
    inbound_stubs.order = _order("payment_submitted")
    ctx = _ctx("text", "paid")
    assert (await payment_inbound._handle_payment_message(ctx)).text == "Received, verification in progress."
    assert (await payment_inbound._handle_payment_message(ctx)).skip_send is True


async def test_image_without_pending_order_falls_through(inbound_stubs):
    """No order awaiting payment → the normal pipeline handles the image."""
    inbound_stubs.order = None
    assert await payment_inbound._handle_payment_message(_ctx("image", "[image]", media_url="u")) is None
    assert inbound_stubs.saved == [] and inbound_stubs.proofs == []


async def test_unrelated_text_falls_through_even_with_pending_order(inbound_stubs):
    """A normal chat message is left to the pipeline."""
    inbound_stubs.order = _order("pending_payment")
    assert await payment_inbound._handle_payment_message(_ctx("text", "do you have this in red?")) is None


async def test_image_download_failure_asks_to_resend_without_proof(inbound_stubs):
    """If the media couldn't be fetched no fake proof row is made."""
    inbound_stubs.order = _order("pending_payment")
    res = await payment_inbound._handle_payment_message(_ctx("image", "[image]", media_url=None))
    assert inbound_stubs.proofs == []
    assert "screenshot" in res.text


async def test_bot_paused_records_proof_but_sends_no_reply(inbound_stubs):
    """While paused the proof is still recorded, but the bot stays silent."""
    inbound_stubs.order = _order("pending_payment")
    ctx = _ctx("image", "[image]", conv=_conv(ai_enabled=False), media_url="https://cdn.test/p.jpg")
    res = await payment_inbound._handle_payment_message(ctx)
    assert inbound_stubs.proofs == ["https://cdn.test/p.jpg"]
    assert res.skip_send is True and res.text is None
    assert [s[0] for s in inbound_stubs.saved] == ["user"]


async def test_bot_paused_skips_paid_text_reply(inbound_stubs):
    """A paused conversation gets no 'send screenshot' auto-reply."""
    inbound_stubs.order = _order("pending_payment")
    res = await payment_inbound._handle_payment_message(_ctx("text", "paid", conv=_conv(ai_enabled=False)))
    assert res.skip_send is True


async def test_pre_process_never_raises(monkeypatch):
    """A failure inside the hook is swallowed so the normal pipeline still answers."""
    async def boom(db, client, conv):
        """Simulated failure."""
        raise RuntimeError("db down")

    monkeypatch.setattr(payment_inbound.conversation_control, "maybe_auto_resume", boom)
    assert await payment_inbound.pre_process(_ctx()) is None


async def test_ingest_media_rehosts_image_and_caches_download(monkeypatch):
    """Inbound images are re-hosted in our storage; the pipeline's own download reuses the bytes."""
    stored = {}

    async def fake_store(client_id, data, ctype, folder="chat"):
        """Capture the upload."""
        stored.update(ctype=ctype, folder=folder)
        return "https://cdn.test/in.jpg"

    monkeypatch.setattr(payment_inbound.media_service, "store_media", fake_store)
    ctx = _ctx("image", "[image]")
    original = ctx.download_media
    original.return_value = b"\xff\xd8\xff\xe0jpegdata"
    await payment_inbound._ingest_media(ctx)
    assert ctx.media_url == "https://cdn.test/in.jpg" and ctx.media_type == "image"
    assert stored["ctype"] == "image/jpeg"
    await ctx.download_media("m1")  # pipeline's later call
    assert original.await_count == 1


# ─── media / realtime / sender helpers ────────────────────────────────────────


def test_sniff_content_type_and_absolute_url(monkeypatch):
    """Magic-byte sniffing + absolute URL building."""
    assert media_service.sniff_content_type(b"\xff\xd8\xff\x00") == "image/jpeg"
    assert media_service.sniff_content_type(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert media_service.sniff_content_type(b"RIFFxxxxWEBPyy") == "image/webp"
    assert media_service.sniff_content_type(b"OggS....") == "audio/ogg"
    assert media_service.sniff_content_type(b"zzzz") == "application/octet-stream"
    monkeypatch.setattr(media_service, "get_settings", lambda: SimpleNamespace(backend_public_url="https://api.x.in/"))
    assert media_service.absolute_url("/uploads/a.png") == "https://api.x.in/uploads/a.png"
    assert media_service.absolute_url("https://cdn/a.png") == "https://cdn/a.png"


async def test_store_media_falls_back_to_local_disk(monkeypatch, tmp_path):
    """Without R2 the bytes land under uploads/{folder}/ and a /uploads URL is returned."""
    monkeypatch.setattr(media_service.storage_service, "upload_file", lambda *a: None)
    monkeypatch.setattr(media_service.storage_service, "_UPLOADS_DIR", str(tmp_path))
    url = await media_service.store_media(5, b"abc", "image/png", folder="chat")
    assert url.startswith("/uploads/chat/5_") and url.endswith(".png")
    assert (tmp_path / "chat" / url.rsplit("/", 1)[1]).read_bytes() == b"abc"


async def test_realtime_publish_is_scoped_to_client_and_survives_full_queue():
    """Events reach only the owning client; a stalled client never blocks publish."""
    a, b = realtime_service.subscribe(1), realtime_service.subscribe(2)
    try:
        realtime_service.publish(1, "payment_submitted", {"order_id": 5})
        assert a.get_nowait() == {"type": "payment_submitted", "data": {"order_id": 5}}
        assert b.empty()
        for _ in range(realtime_service._QUEUE_MAX + 5):
            realtime_service.publish(1, "new_message", {})  # must not raise
        realtime_service.publish(None, "x", {})
    finally:
        realtime_service.unsubscribe(1, a)
        realtime_service.unsubscribe(2, b)
    assert realtime_service.subscriber_count(1) == 0


async def test_channel_sender_whatsapp_image_and_persist(monkeypatch):
    """A WhatsApp image send goes through outbound.send_image and is persisted as human."""
    seen = {}

    async def send_image(to, url, caption, **kw):
        """Capture the outbound call."""
        seen.update(to=to, url=url, caption=caption, kind=kw["kind"], pid=kw["phone_number_id"])
        return {"messages": [{"id": "x"}]}

    async def save(db, conv_id, role, content, **kw):
        """Capture the persisted row."""
        seen["saved"] = (role, content, kw)
        return SimpleNamespace(id=1, conversation_id=conv_id, role=role, content=content, direction="outbound",
                               sender_type=kw["sender_type"], sender_user_id=kw["sender_user_id"], channel="whatsapp",
                               media_url=kw["media_url"], media_type=kw["media_type"], created_at=None)

    monkeypatch.setattr(channel_sender.outbound, "send_image", send_image)
    monkeypatch.setattr(channel_sender.conversation_service, "save_message", save)
    out = await channel_sender.send_to_conversation(
        _db(), _client(), _conv(), text="here you go", image_url="https://cdn/x.jpg",
        kind=channel_sender.MessageKind.MANUAL_AGENT, sender_type="human", sender_user_id=9)
    assert out.sent
    assert seen["to"] == "919876543210" and seen["pid"] == "pn" and seen["caption"] == "here you go"
    role, content, kw = seen["saved"]
    assert (role, kw["sender_type"], kw["sender_user_id"], kw["media_type"]) == ("assistant", "human", 9, "image")


async def test_channel_sender_instagram_routes_to_dm(monkeypatch):
    """Instagram conversations use the IG sender with the client's IG account id."""
    async def ig_dm(ig_id, igsid, text, **kw):
        """Capture IG send."""
        assert (ig_id, igsid, text) == ("ig1", "igsid9", "hi")
        return {"message_id": "m"}

    async def save(db, conv_id, role, content, **kw):
        """Return a persisted stub."""
        return SimpleNamespace(id=2, conversation_id=conv_id, role=role, content=content, direction="outbound",
                               sender_type="human", sender_user_id=None, channel="instagram", media_url=None,
                               media_type=None, created_at=None)

    monkeypatch.setattr(channel_sender.outbound, "ig_send_dm", ig_dm)
    monkeypatch.setattr(channel_sender.conversation_service, "save_message", save)
    out = await channel_sender.send_to_conversation(
        _db(), _client(instagram_account_id="ig1"), _conv(channel="instagram", phone_number="igsid9"),
        text="hi", kind=channel_sender.MessageKind.MANUAL_AGENT, sender_type="human")
    assert out.sent


async def test_channel_sender_reports_suppression_and_missing_ig_account(monkeypatch):
    """Gate suppression → SUPPRESSED (nothing persisted); IG without an account → NO_CHANNEL."""
    async def suppressed(*a, **k):
        """Gate said no."""
        return None

    monkeypatch.setattr(channel_sender.outbound, "send_text", suppressed)
    out = await channel_sender.send_to_conversation(
        _db(), _client(), _conv(), text="x", kind=channel_sender.MessageKind.MANUAL_AGENT, sender_type="human")
    assert (out.sent, out.reason) == (False, "SUPPRESSED")
    out = await channel_sender.send_to_conversation(
        _db(), _client(), _conv(channel="instagram"), text="x",
        kind=channel_sender.MessageKind.MANUAL_AGENT, sender_type="human")
    assert (out.sent, out.reason) == (False, "NO_CHANNEL")


async def test_channel_sender_transport_error_is_send_failed(monkeypatch):
    """A Meta transport exception becomes SEND_FAILED instead of propagating."""
    async def boom(*a, **k):
        """Transport failure."""
        raise RuntimeError("meta 500")

    monkeypatch.setattr(channel_sender.outbound, "send_text", boom)
    out = await channel_sender.send_to_conversation(
        _db(), _client(), _conv(), text="x", kind=channel_sender.MessageKind.MANUAL_AGENT, sender_type="human")
    assert (out.sent, out.reason) == (False, "SEND_FAILED")


async def test_precheck_maps_gate_verdicts(monkeypatch):
    """precheck: ALLOW → None, window closed → WINDOW_CLOSED, opt-out/blocked → codes."""
    from app.services.send_gate import DenyReason, SendDecision, Verdict

    verdicts = iter([
        SendDecision(Verdict.ALLOW),
        SendDecision(Verdict.ALLOW_TEMPLATE_ONLY, DenyReason.WINDOW_CLOSED),
        SendDecision(Verdict.DENY, DenyReason.OPTED_OUT),
        SendDecision(Verdict.DENY, DenyReason.BLOCKED),
    ])

    async def fake_check(*a, **k):
        """Return the next scripted verdict."""
        return next(verdicts)

    async def fake_customer(db, client_id, phone):
        """No customer row needed."""
        return None

    monkeypatch.setattr(channel_sender.send_gate, "check_send", fake_check)
    from app.services import customer_service
    monkeypatch.setattr(customer_service, "get_customer", fake_customer)
    kind = channel_sender.MessageKind.MANUAL_AGENT
    got = [await channel_sender.precheck(_db(), _client(), _conv(), kind) for _ in range(4)]
    assert got == [None, "WINDOW_CLOSED", "OPTED_OUT", "BLOCKED"]


# ─── API contract (router level) ──────────────────────────────────────────────


def _api(monkeypatch, router_module, user):
    """TestClient over one router with auth dependencies replaced by `user`."""
    from app.db import get_db
    from app.routers import auth

    app = FastAPI()
    app.include_router(router_module.router)
    db = _db()

    async def _get_db():
        """Yield the stub session."""
        yield db

    async def _user():
        """Authenticated user."""
        return user

    app.dependency_overrides[get_db] = _get_db
    for dep in (getattr(router_module, "_perm", None), auth.get_owner_client, auth.get_current_client):
        if dep is not None:
            app.dependency_overrides[dep] = (lambda: user.client) if dep in (auth.get_owner_client, auth.get_current_client) else _user
    return TestClient(app), db


def _user(**kw):
    """Authenticated staff user with an attached client."""
    u = SimpleNamespace(id=9, client_id=1, email="owner@shop.com", client=_client(**kw))
    return u


def test_payment_settings_rejects_invalid_upi(monkeypatch):
    """PUT /settings/payment 422s with INVALID_UPI_ID for a malformed handle."""
    from app.routers import payment_settings

    api, _ = _api(monkeypatch, payment_settings, _user())
    resp = api.put("/settings/payment", json={"upi_id": "not-a-upi"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "INVALID_UPI_ID"


def test_payment_settings_saves_upi_clears_alert_and_returns_preview(monkeypatch):
    """A valid UPI ID is saved, the missing-UPI alert is cleared, and a 3-language preview is returned."""
    from app.routers import payment_settings

    user = _user(upi_id=None, payment_setup_alert_at=datetime.now(timezone.utc))
    api, db = _api(monkeypatch, payment_settings, user)
    resp = api.put("/settings/payment", json={"upi_id": "shop@okaxis", "upi_payee_name": "Riya Sarees",
                                              "bot_auto_resume_minutes": 45})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upi_id"] == "shop@okaxis" and body["upi_configured"] is True
    assert body["setup_alert_active"] is False and body["bot_auto_resume_minutes"] == 45
    assert set(body["preview"]) == {"en", "hi", "gu"}
    assert "shop@okaxis" in body["preview"]["en"] and "After payment" in body["preview"]["en"]
    assert user.client.payment_setup_alert_at is None
    db.commit.assert_awaited()


def test_payment_settings_get_flags_missing_upi_alert(monkeypatch):
    """GET reports an active alert while no UPI ID is configured."""
    from app.routers import payment_settings

    user = _user(upi_id=None, payment_setup_alert_at=datetime.now(timezone.utc))
    api, _ = _api(monkeypatch, payment_settings, user)
    body = api.get("/settings/payment").json()
    assert body["upi_configured"] is False and body["setup_alert_active"] is True


def test_payment_settings_upload_rejects_non_image(monkeypatch):
    """Static QR upload refuses non-image files."""
    from app.routers import payment_settings

    api, _ = _api(monkeypatch, payment_settings, _user())
    resp = api.post("/settings/payment/qr", files={"file": ("x.txt", b"hello", "text/plain")})
    assert resp.status_code == 422


def test_approve_endpoint_maps_invalid_state_to_409(monkeypatch):
    """Approving an order that isn't payment_submitted → 409 INVALID_STATE with the edge."""
    from app.routers import payment_verification as pv

    async def fake_approve(db, client, order_id, user):
        """Service refuses."""
        raise InvalidOrderTransition("pending_payment", "paid")

    monkeypatch.setattr(pv.pvs, "approve", fake_approve)
    api, _ = _api(monkeypatch, pv, _user())
    resp = api.post("/orders/7/payment/approve")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "INVALID_STATE", "message": "Order cannot move from 'pending_payment' to 'paid'.",
        "current": "pending_payment", "target": "paid",
    }


def test_approve_endpoint_404_for_unknown_order(monkeypatch):
    """Unknown/other-tenant order → 404 ORDER_NOT_FOUND."""
    from app.routers import payment_verification as pv

    async def fake_approve(db, client, order_id, user):
        """Service can't find it."""
        raise pvs.OrderNotFound(order_id)

    monkeypatch.setattr(pv.pvs, "approve", fake_approve)
    api, _ = _api(monkeypatch, pv, _user())
    assert api.post("/orders/99/payment/approve").json()["detail"]["code"] == "ORDER_NOT_FOUND"


def test_approve_and_reject_endpoints_return_decision_shape(monkeypatch):
    """Approve/reject responses expose changed + customer_notified for the UI."""
    from app.routers import payment_verification as pv

    order = _order("paid")

    async def fake_approve(db, client, order_id, user):
        """Service approves."""
        return pvs.DecisionResult(order, changed=True, customer_notified=True)

    captured = {}

    async def fake_reject(db, client, order_id, user, reason=None):
        """Service rejects."""
        captured["reason"] = reason
        return pvs.DecisionResult(_order("pending_payment"), changed=True, customer_notified=False,
                                  notify_failure="WINDOW_CLOSED")

    monkeypatch.setattr(pv.pvs, "approve", fake_approve)
    monkeypatch.setattr(pv.pvs, "reject", fake_reject)
    api, _ = _api(monkeypatch, pv, _user())
    ok = api.post("/orders/7/payment/approve").json()
    assert ok == {"order_id": 7, "order_number": "ORD-2026-0007", "status": "paid", "changed": True,
                  "customer_notified": True, "notify_failure": None}
    rej = api.post("/orders/7/payment/reject", json={"reason": "Amount mismatch"}).json()
    assert captured["reason"] == "Amount mismatch"
    assert rej["status"] == "pending_payment" and rej["notify_failure"] == "WINDOW_CLOSED"
    api.post("/orders/7/payment/reject")  # body is optional
    assert captured["reason"] is None


def test_pending_count_endpoint(monkeypatch):
    """GET /payments/pending/count returns the badge number."""
    from app.routers import payment_verification as pv

    async def fake_count(db, client_id):
        """Three waiting."""
        return 3

    monkeypatch.setattr(pv.pvs, "count_pending", fake_count)
    api, _ = _api(monkeypatch, pv, _user())
    assert api.get("/payments/pending/count").json() == {"count": 3}


def test_pending_list_endpoint_shapes_rows_and_flags_overdue(monkeypatch):
    """The list endpoint returns waiting time, overdue flag, proofs and seller UPI."""
    from app.routers import payment_verification as pv

    since = datetime.now(timezone.utc) - timedelta(hours=3)
    proof = SimpleNamespace(id=1, order_id=7, message_id=5, media_url="https://cdn/p.jpg", status="pending",
                            created_at=since, reviewed_by_name=None, reviewed_at=None, reject_reason=None)
    order = _order("payment_submitted", customer_name="Asha", customer_phone="9198", product_name="Kurta")
    captured = {}

    async def fake_list(db, client_id, **kw):
        """Return one pending row."""
        captured.update(kw)
        return [pvs.PendingPayment(order=order, channel="whatsapp", proofs=[proof], waiting_since=since)]

    monkeypatch.setattr(pv.pvs, "list_payments", fake_list)
    api, _ = _api(monkeypatch, pv, _user())
    rows = api.get("/payments/pending?status=pending&from=2026-01-01&to=2026-02-01&search=asha").json()
    assert captured["status"] == "pending" and str(captured["date_from"]) == "2026-01-01"
    assert captured["search"] == "asha"
    row = rows[0]
    assert row["order_id"] == 7 and row["amount_expected"] == 1299.0 and row["channel"] == "whatsapp"
    assert row["overdue"] is True and row["waiting_seconds"] >= 3 * 3600 - 5
    assert row["seller_upi_id"] == "shop@okaxis" and row["proofs"][0]["media_url"] == "https://cdn/p.jpg"
    assert row["items"] == [{"name": "Kurta", "variant": None, "quantity": 1, "subtotal": 1299.0}]
    assert api.get("/payments/pending?status=bogus").status_code == 422


def _conv_api(monkeypatch, conv, **client_kw):
    """TestClient over the conversations router with a stubbed conversation lookup."""
    from app.routers import conversations as c

    user = _user(**client_kw)
    api, db = _api(monkeypatch, c, user)

    async def get_conv(db_, client_id, conv_id):
        """Return the staged conversation."""
        return conv

    monkeypatch.setattr(c, "_get_conv_or_404", get_conv)

    async def no_dup(*a, **k):
        """Idempotency lookup finds nothing."""
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    db.execute = no_dup
    return api, db, user, c


def test_send_outside_24h_returns_window_closed_with_templates(monkeypatch):
    """Free-form send with the window closed → 409 WINDOW_CLOSED + approved templates; nothing sent."""
    conv = _conv()
    api, db, user, c = _conv_api(monkeypatch, conv)
    sent = AsyncMock()

    async def precheck(db_, client, conv_, kind):
        """Gate says window closed."""
        return "WINDOW_CLOSED"

    async def templates(db_, client_id):
        """One approved utility template."""
        return [c.TemplateOut(id=1, name="order_update", language="en", category="utility", body="Hi {{1}}")]

    monkeypatch.setattr(c.channel_sender, "precheck", precheck)
    monkeypatch.setattr(c.channel_sender, "send_to_conversation", sent)
    monkeypatch.setattr(c, "_approved_templates", templates)
    resp = api.post("/conversations/3/messages", json={"text": "hello"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "WINDOW_CLOSED"
    assert detail["templates"] == [{"id": 1, "name": "order_update", "language": "en", "category": "utility", "body": "Hi {{1}}"}]
    sent.assert_not_awaited()
    assert conv.ai_enabled is True  # a refused send must not pause the bot


@pytest.mark.parametrize("code", ["OPTED_OUT", "BLOCKED"])
def test_send_respects_opt_out_and_block(monkeypatch, code):
    """Opted-out / blocked customers → 409 with that code."""
    api, _, _, c = _conv_api(monkeypatch, _conv())

    async def precheck(*a, **k):
        """Gate refuses."""
        return code

    monkeypatch.setattr(c.channel_sender, "precheck", precheck)
    resp = api.post("/conversations/3/messages", json={"text": "hello"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == code


def test_send_inside_window_persists_as_human_and_pauses_bot(monkeypatch):
    """In-window send → sender_type human, bot paused with auto-resume time returned."""
    conv = _conv()
    api, db, user, c = _conv_api(monkeypatch, conv)
    captured = {}

    async def precheck(*a, **k):
        """Window open."""
        return None

    async def send(db_, client, conv_, **kw):
        """Pretend Meta accepted it."""
        captured.update(kw)
        msg = SimpleNamespace(id=11, conversation_id=3, role="assistant", content=kw["text"], direction="outbound",
                              sender_type="human", sender_user_id=9, channel="whatsapp", media_url=None,
                              media_type=None, created_at=datetime.now(timezone.utc), wamid=None)
        return channel_sender.SendOutcome(True, None, msg)

    monkeypatch.setattr(c.channel_sender, "precheck", precheck)
    monkeypatch.setattr(c.channel_sender, "send_to_conversation", send)
    resp = api.post("/conversations/3/messages", json={"text": "Your order ships today"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert captured["sender_type"] == "human" and captured["sender_user_id"] == 9
    assert captured["kind"] == channel_sender.MessageKind.MANUAL_AGENT
    assert body["message"]["sender_type"] == "human" and body["message"]["direction"] == "outbound"
    assert body["message"]["sender_name"] == "owner@shop.com"
    assert body["bot_paused"] is True and body["auto_resume_at"] is not None
    assert conv.ai_enabled is False and conv.bot_pause_source == "human_send"


def test_send_failure_is_502_and_does_not_pause(monkeypatch):
    """A transport failure → 502 SEND_FAILED; the bot is not paused."""
    conv = _conv()
    api, _, _, c = _conv_api(monkeypatch, conv)

    async def precheck(*a, **k):
        """Window open."""
        return None

    async def send(*a, **k):
        """Meta rejected."""
        return channel_sender.SendOutcome(False, "SEND_FAILED")

    monkeypatch.setattr(c.channel_sender, "precheck", precheck)
    monkeypatch.setattr(c.channel_sender, "send_to_conversation", send)
    resp = api.post("/conversations/3/messages", json={"text": "hello"})
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "SEND_FAILED"
    assert conv.ai_enabled is True


def test_send_requires_a_payload(monkeypatch):
    """Empty body → 422."""
    api, _, _, _ = _conv_api(monkeypatch, _conv())
    assert api.post("/conversations/3/messages", json={}).status_code == 422
    assert api.post("/conversations/3/messages", json={"text": "   "}).status_code == 422


def test_send_template_requires_whatsapp(monkeypatch):
    """Templates are WhatsApp-only."""
    api, _, _, _ = _conv_api(monkeypatch, _conv(channel="instagram"))
    resp = api.post("/conversations/3/messages", json={"template": {"name": "order_update"}})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "TEMPLATES_WHATSAPP_ONLY"


def test_takeover_and_resume_toggle_the_bot(monkeypatch):
    """PATCH takeover pauses (source=manual, never auto-resumes); PATCH resume re-enables."""
    conv = _conv()
    api, db, user, c = _conv_api(monkeypatch, conv)

    async def fake_detail(conv_id, before_id, limit, user_, db_):
        """Echo bot state in a minimal valid detail payload."""
        return c.ConversationDetail(
            id=conv_id, phone_number="1", channel="whatsapp", customer_name=None,
            ai_enabled=conv.ai_enabled is not False, bot_paused=conv.ai_enabled is False,
            bot_pause_source=conv.bot_pause_source, auto_resume_at=None, taken_over_at=None,
            taken_over_note=conv.taken_over_note, lead_status="cold", message_count=0, created_at="",
            updated_at=None, window=c.WindowOut(open=False, closes_at=None, seconds_left=0),
            opted_out=False, messages=[], has_more=False, events=[], current_order=None, orders=[],
        )

    monkeypatch.setattr(c, "get_conversation", fake_detail)
    paused = api.patch("/conversations/3/takeover", json={"note": "on call"}).json()
    assert paused["bot_paused"] is True and paused["bot_pause_source"] == "manual"
    assert paused["taken_over_note"] == "on call"
    assert conversation_control.auto_resume_at(conv, user.client) is None
    resumed = api.patch("/conversations/3/resume").json()
    assert resumed["bot_paused"] is False and conv.ai_enabled is True


# ─── customer-side cancel + legacy status PATCH guard ─────────────────────────


async def test_cancel_by_customer_releases_reservation_and_audits(stubbed_service, monkeypatch):
    """The in-chat cancel path releases stock and records a 'customer' audit row; no template is sent."""
    order = _order("pending_payment", client_id=1)
    await pvs.cancel_by_customer(_db(), order)
    assert order.status == "cancelled" and order.cancel_reason == "cancelled by customer"
    assert order.cancelled_at is not None
    assert stubbed_service.released == 1 and stubbed_service.notified == []
    assert stubbed_service.audits == ["order_cancelled"]


async def test_cancel_by_customer_refuses_orders_with_a_screenshot(stubbed_service):
    """payment_submitted orders must be decided by the seller, never auto-cancelled by chat."""
    with pytest.raises(InvalidOrderTransition) as exc:
        await pvs.cancel_by_customer(_db(), _order("paid"))
    assert exc.value.current == "paid"


def test_status_patch_blocks_manual_paid_for_upi_orders_in_payment_flow(monkeypatch):
    """PATCH status=paid on a pending_payment UPI order → 409 USE_PAYMENT_APPROVAL (no bypass of verification)."""
    from app.routers import orders as o
    from app.routers.auth import get_current_client

    order = _order("pending_payment")
    user = _user()
    user.has_permission = lambda key: True
    app = FastAPI()
    app.include_router(o.router)
    from app.db import get_db
    import inspect

    perm_dep = inspect.signature(o.update_status).parameters["_perm"].default.dependency

    async def _gdb():
        """Stub session."""
        yield _db()

    async def get_order(order_id, client_id, db_):
        """Return the staged order."""
        return order

    monkeypatch.setattr(o.order_service, "get_order", get_order)
    app.dependency_overrides[get_db] = _gdb
    app.dependency_overrides[get_current_client] = lambda: user.client
    app.dependency_overrides[perm_dep] = lambda: user
    api = TestClient(app)
    resp = api.patch("/orders/7/status", json={"status": "paid"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "USE_PAYMENT_APPROVAL"

    called = {}

    async def fake_cancel(db, client, order_id, actor, reason=None):
        """Service cancel."""
        called["reason"] = reason
        full = _order(
            "cancelled", customer_name="Asha", customer_phone="9198", delivery_address="x", mobile_number=None,
            product_id=1, product_sku="S", variant_material=None, unit_price=1299.0, razorpay_payment_id=None,
            tracking_number=None, courier_name=None, created_at=None, dispatched_at=None, delivered_at=None,
            notes=None, invoice_url=None, invoice_number=None,
        )
        return pvs.DecisionResult(full, changed=True)

    monkeypatch.setattr(o.payment_verification_service, "cancel", fake_cancel)
    resp = api.patch("/orders/7/status", json={"status": "cancelled", "notes": "customer unreachable"})
    assert resp.status_code == 200 and called["reason"] == "customer unreachable"


# ─── per-client media tokens ──────────────────────────────────────────────────


async def test_webhook_media_downloader_binds_the_business_token():
    """Embedded-Signup clients download media with THEIR token; others fall back to the global default."""
    from app.routers import webhook

    seen = []

    async def fake_download(media_id, access_token=None):
        """Record what the transport would be called with."""
        seen.append((media_id, access_token))
        return b"x"

    await webhook._media_downloader(fake_download, "client-token")("m1")
    await webhook._media_downloader(fake_download, None)("m2")
    assert seen == [("m1", "client-token"), ("m2", None)]


async def test_instagram_media_downloader_binds_the_business_token(monkeypatch):
    """Instagram downloads use the business's own token when it has one."""
    from app.routers import instagram

    seen = []

    async def fake_download(url, access_token=None):
        """Record the call."""
        seen.append((url, access_token))
        return b"x"

    monkeypatch.setattr(instagram.vision_service, "download_instagram_media", fake_download)
    await instagram._ig_media_downloader("ig-token")("https://cdn/x")
    await instagram._ig_media_downloader(None)("https://cdn/y")
    assert seen == [("https://cdn/x", "ig-token"), ("https://cdn/y", None)]


async def test_vision_download_uses_supplied_token_in_auth_header(monkeypatch):
    """vision_service sends the per-client token as the Bearer header."""
    from app.services import vision_service

    headers_seen = []

    class FakeResp:
        """Minimal httpx response."""
        content = b"img"

        def raise_for_status(self):
            """OK."""

        def json(self):
            """Meta media lookup payload."""
            return {"url": "https://cdn/real.jpg"}

    class FakeClient:
        """Minimal AsyncClient that records request headers."""
        def __init__(self, *a, **k):
            """Ignore client options."""

        async def __aenter__(self):
            """Enter context."""
            return self

        async def __aexit__(self, *a):
            """Exit context."""

        async def get(self, url, headers=None):
            """Record headers."""
            headers_seen.append(headers["Authorization"])
            return FakeResp()

    monkeypatch.setattr(vision_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(vision_service, "get_settings", lambda: SimpleNamespace(
        whatsapp_access_token="global-wa", instagram_access_token="global-ig"))
    assert await vision_service.download_whatsapp_media("m", access_token="tenant") == b"img"
    assert await vision_service.download_whatsapp_media("m") == b"img"
    assert await vision_service.download_instagram_media("https://x", access_token="tenant-ig") == b"img"
    assert headers_seen == ["Bearer tenant", "Bearer tenant", "Bearer global-wa", "Bearer global-wa", "Bearer tenant-ig"]
