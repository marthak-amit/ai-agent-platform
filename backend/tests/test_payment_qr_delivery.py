"""WhatsApp adapter: the payment QR's caption carries the closing line, so a failed QR send must fall back to text."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers import _whatsapp_adapter as adapter
from app.services.order_pipeline import PipelineResult, is_free_text_slot_answer

CLOSING = "After payment, please send the payment screenshot here."


def _result():
    """The payment-instruction result: text without the closing line + the QR whose caption is the closing line."""
    return PipelineResult(
        text="Order #1\nPay to UPI ID: shop@upi", images=[("https://cdn.test/qr.png", CLOSING)],
        image_fallback_text=CLOSING,
    )


@pytest.fixture
def out(monkeypatch):
    """Stub the gated outbound sends and return the mocks."""
    mocks = SimpleNamespace(text=AsyncMock(return_value={"ok": 1}), image=AsyncMock(return_value={"ok": 1}))
    monkeypatch.setattr(adapter.outbound, "send_text", mocks.text)
    monkeypatch.setattr(adapter.outbound, "send_image", mocks.image)
    return mocks


async def _send(result):
    """Run the adapter for one conversation."""
    await adapter.send_pipeline_result(result, db=None, conv=SimpleNamespace(id=5, client_id=1), sender_phone="9199", pid="p")


async def test_qr_delivered_sends_no_fallback_text_and_logs(out, caplog):
    """QR image sent → the caption is the closing line; nothing extra is sent, and the send is logged."""
    with caplog.at_level(logging.INFO, logger="app.routers.webhook"):
        await _send(_result())
    assert out.image.await_count == 1 and out.text.await_count == 1          # main text only
    assert "Image sent to conv=5" in caplog.text


async def test_qr_suppressed_by_send_gate_falls_back_to_closing_text(out, caplog):
    """send_image returns None (gate suppressed it) → the closing line is sent as text and a warning is logged."""
    out.image.return_value = None
    with caplog.at_level(logging.INFO, logger="app.routers.webhook"):
        await _send(_result())
    assert [c.args[1] for c in out.text.await_args_list] == ["Order #1\nPay to UPI ID: shop@upi", CLOSING]
    assert "Image NOT sent" in caplog.text and "closing line as text" in caplog.text


async def test_qr_rejected_by_meta_falls_back_to_closing_text(out, caplog):
    """The Meta media fetch fails (HTTP error) → fallback text + an ERROR naming the url."""
    out.image.side_effect = RuntimeError("Meta 400: media URI doesn't resolve")
    with caplog.at_level(logging.INFO, logger="app.routers.webhook"):
        await _send(_result())
    assert out.text.await_args_list[-1].args[1] == CLOSING
    assert "Image send error" in caplog.text and "https://cdn.test/qr.png" in caplog.text


async def test_product_images_never_trigger_the_fallback(out):
    """Ordinary product photos have no fallback text — a failed photo send must not add a message."""
    out.image.return_value = None
    await _send(PipelineResult(text="card", images=[("https://cdn.test/p.png", "Saree")]))
    assert out.text.await_count == 1


def test_free_text_slot_answers_are_recognised():
    """A saved name/address (even re-typed slightly differently) is a slot answer, not language evidence."""
    conv = SimpleNamespace(customer_name="Karan Mehta", delivery_address="9 Ellis Bridge, Ahmedabad 380006")
    assert is_free_text_slot_answer(conv, "9 Ellis Bridge, Ahmedabad 380006")
    assert is_free_text_slot_answer(conv, "karan  mehta")
    assert not is_free_text_slot_answer(conv, "kya price hai")
    assert not is_free_text_slot_answer(SimpleNamespace(customer_name=None, delivery_address=None), "anything")
