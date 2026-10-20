"""No-DB tests for the simulator fences: sandbox identity, outbound refusal, tap mapping, sandbox order side effects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import order_service, outbound
from app.services.order_pipeline import _encode_btn
from app.services.sandbox_guard import is_sandbox_phone, sandbox_phone
from app.services.send_gate import MessageKind
from app.services.simulator_service import interactive_to_text


def test_sandbox_phone_roundtrip():
    """sandbox_phone() builds the key and is_sandbox_phone() recognises exactly that family."""
    assert sandbox_phone(7) == "sandbox_7"
    assert is_sandbox_phone("sandbox_7") and is_sandbox_phone(sandbox_phone(123))
    for real in ("919999999999", "17841400000000000", "", None, 5, "Sandbox_1", "x_sandbox_1"):
        assert not is_sandbox_phone(real)


@pytest.mark.parametrize("sender", ["send_text", "send_buttons", "send_list", "send_image", "send_document", "send_template"])
async def test_outbound_never_reaches_the_channel_for_the_sandbox_customer(monkeypatch, sender):
    """Every WhatsApp send function refuses `sandbox_<id>` before any transport call."""
    raw = AsyncMock(return_value={"ok": True})
    for name in ("_raw_send_text_message", "_raw_send_button_message", "_raw_send_list_message",
                 "_raw_send_image_message", "_raw_send_document_message", "_raw_send_template_message"):
        monkeypatch.setattr(f"app.services.whatsapp_service.{name}", raw)
    kw = dict(kind=MessageKind.PIPELINE_REPLY)
    args = {
        "send_text": ("sandbox_1", "hi"),
        "send_buttons": ("sandbox_1", "hi", [{"id": "a", "title": "A"}]),
        "send_list": ("sandbox_1", "h", "b", "btn", []),
        "send_image": ("sandbox_1", "https://x/y.png", "cap"),
        "send_document": ("sandbox_1", "https://x/y.pdf", "y.pdf", "cap"),
        "send_template": ("sandbox_1", "tmpl"),
    }[sender]
    result = await getattr(outbound, sender)(*args, **kw)
    assert not result
    raw.assert_not_called()


BUTTON_CASES = [
    ("confirm_pay", "Confirm & Pay", "confirm"),
    ("cancel_order", "Cancel", "cancel"),
    ("confirm_no", "No", "cancel"),
    ("paid_done", "I've Paid", "paid"),
    ("offer_yes", "Yes", "yes"),
    ("offer_no", "No", "no"),
    ("upi", "UPI", "UPI"),
    ("cod", "Cash on Delivery", "COD"),
    ("bank_transfer", "Bank", "bank transfer"),
    ("SR20001", "Green Saree", "SR20001"),
    ("something_else", "Pink", "Pink"),
]


@pytest.mark.parametrize("button_id, title, expected", BUTTON_CASES)
def test_button_tap_maps_like_the_whatsapp_webhook(button_id, title, expected):
    """A tapped button becomes the same canonical text routers/webhook.py feeds the pipeline."""
    assert interactive_to_text("button", button_id, title) == (expected, None)


def test_nonce_encoded_button_is_decoded_and_the_nonce_passed_on():
    """Production encodes ids as action~conv~nonce; the stale-tap guard needs the decoded tuple."""
    encoded = _encode_btn("confirm_pay", 42, "abc12345")
    text, parsed = interactive_to_text("button", encoded, "Confirm & Pay")
    assert text == "confirm" and parsed == ("confirm_pay", "42", "abc12345")


def test_list_row_sku_or_title():
    """A list row whose id is a SKU resolves to the SKU; any other row falls back to its title."""
    assert interactive_to_text("list", _encode_btn("SR20001", 9, "n0nce000"), "Green")[0] == "SR20001"
    assert interactive_to_text("list", "opt_3", "Third thing")[0] == "Third thing"


def _sandbox_order(**over):
    """A sandbox Order stand-in carrying just what the order lifecycle reads."""
    return SimpleNamespace(
        order_number="SIM-2026-0001", is_sandbox=True, stock_deducted=False, status="pending_payment",
        payment_status="pending", paid_at=None, confirmed_at=None, line_items=[], product_id=1, **over,
    )


async def test_marking_a_sandbox_order_paid_touches_no_stock_and_sends_nothing(monkeypatch):
    """mark_order_paid on a simulator order flips status only: no stock deduction, owner alert or invoice."""
    deduct = AsyncMock()
    notify = AsyncMock()
    invoice = AsyncMock()
    monkeypatch.setattr(order_service, "_deduct_product_stock", deduct)
    monkeypatch.setattr(order_service, "_notify_owner_new_order", notify)
    monkeypatch.setattr(order_service, "_generate_and_send_invoice", invoice)
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    order = _sandbox_order()

    assert await order_service.mark_order_paid(db, order, SimpleNamespace(phone="919")) is True

    assert order.status == "paid" and order.payment_status == "paid"
    deduct.assert_not_called()
    notify.assert_not_called()
    invoice.assert_not_called()


async def test_stock_deduction_is_a_noop_for_sandbox_orders():
    """apply_stock_deduction never queries products for a simulator order."""
    db = MagicMock(execute=AsyncMock())
    order = _sandbox_order()
    await order_service.apply_stock_deduction(db, order)
    db.execute.assert_not_called()
    assert order.stock_deducted is True


async def test_owner_notifications_skip_sandbox_orders(monkeypatch):
    """The owner is never WhatsApped about the order they are testing themselves."""
    sent = AsyncMock()
    monkeypatch.setattr("app.services.outbound.send_owner_text", sent)
    order = _sandbox_order(customer_name="X", customer_phone="sandbox_1", delivery_address="a",
                           payment_method="UPI", total_amount=10, quantity=1, product_name="P")
    client = SimpleNamespace(phone="919999999999")
    await order_service._notify_owner_new_order(order, client)
    await order_service._notify_owner_invoice_failed(order, client)
    sent.assert_not_called()
