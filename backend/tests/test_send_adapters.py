"""
Tests for app/routers/_whatsapp_adapter.py and _instagram_adapter.py's
pre_images handling — the image-request feature needs the photo delivered as
a separate message BEFORE the main text reply (the existing `images` field
sends AFTER text instead), so these confirm ordering explicitly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers import _instagram_adapter, _whatsapp_adapter
from app.services.order_pipeline import CarouselItemSpec, PipelineResult
from app.services.send_gate import MessageKind


@pytest.fixture
def conv():
    c = MagicMock()
    c.id = 1
    c.client_id = 1
    return c


# ── WhatsApp adapter ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whatsapp_adapter_sends_pre_image_before_text(conv):
    """pre_images fires outbound.send_image before outbound.send_text."""
    call_order = []

    def _mark(name):
        call_order.append(name)

    with patch("app.routers._whatsapp_adapter.outbound") as mock_outbound:
        mock_outbound.send_image = AsyncMock(side_effect=lambda *a, **k: _mark("send_image"))
        mock_outbound.send_text = AsyncMock(side_effect=lambda *a, **k: _mark("send_text"))

        result = PipelineResult(
            text="Cotton Saree [SR001] — ₹1500\n\nWould you like to order? (Yes / No)",
            pre_images=[("https://media.example.com/1/1/a.jpg", None)],
        )
        await _whatsapp_adapter.send_pipeline_result(
            result, db=None, conv=conv, sender_phone="919876543210", pid=None,
        )

    assert call_order == ["send_image", "send_text"]
    mock_outbound.send_image.assert_called_once_with(
        "919876543210", "https://media.example.com/1/1/a.jpg", None,
        kind=MessageKind.PIPELINE_REPLY, db=None, client_id=1, conversation_id=1,
    )


@pytest.mark.asyncio
async def test_whatsapp_adapter_no_pre_images_unaffected(conv):
    """Default pre_images=None behaves exactly as before — no image send attempted."""
    with patch("app.routers._whatsapp_adapter.outbound") as mock_outbound:
        mock_outbound.send_image = AsyncMock()
        mock_outbound.send_text = AsyncMock()

        result = PipelineResult(text="Hello")
        await _whatsapp_adapter.send_pipeline_result(
            result, db=None, conv=conv, sender_phone="919876543210", pid=None,
        )

    mock_outbound.send_image.assert_not_called()
    mock_outbound.send_text.assert_called_once()


# ── Instagram adapter ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_instagram_adapter_sends_pre_image_before_text(conv):
    """pre_images fires outbound.ig_send_image before outbound.ig_send_dm (main text)."""
    call_order = []

    def _mark(name):
        call_order.append(name)

    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_image = AsyncMock(side_effect=lambda *a, **k: _mark("ig_send_image"))
        mock_outbound.ig_send_dm = AsyncMock(side_effect=lambda *a, **k: _mark("ig_send_dm"))

        result = PipelineResult(
            text="Cotton Saree [SR001] — ₹1500\n\nWould you like to order? (Yes / No)",
            pre_images=[("https://media.example.com/1/1/a.jpg", None)],
        )
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    assert call_order == ["ig_send_image", "ig_send_dm"]


@pytest.mark.asyncio
async def test_instagram_adapter_sends_pre_image_caption_as_separate_dm(conv):
    """A pre_images caption goes out as its own ig_send_dm, after the image (IG has no image-caption field)."""
    call_order = []

    def _mark(name):
        call_order.append(name)

    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_image = AsyncMock(side_effect=lambda *a, **k: _mark("ig_send_image"))
        mock_outbound.ig_send_dm = AsyncMock(side_effect=lambda *a, **k: _mark("ig_send_dm"))

        result = PipelineResult(
            text="main text",
            pre_images=[("https://media.example.com/1/1/a.jpg", "a caption")],
        )
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    # caption dm (from pre_images) then main text dm — both are ig_send_dm calls.
    assert call_order == ["ig_send_image", "ig_send_dm", "ig_send_dm"]
    assert mock_outbound.ig_send_dm.call_args_list[0].args[2] == "a caption"
    assert mock_outbound.ig_send_dm.call_args_list[1].args[2] == "main text"


@pytest.mark.asyncio
async def test_instagram_adapter_no_pre_images_unaffected(conv):
    """Default pre_images=None behaves exactly as before — no image send attempted."""
    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_image = AsyncMock()
        mock_outbound.ig_send_dm = AsyncMock()

        result = PipelineResult(text="Hello")
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    mock_outbound.ig_send_image.assert_not_called()
    mock_outbound.ig_send_dm.assert_called_once()


# ── Instagram carousel (multi-product match) ────────────────────────────────

@pytest.mark.asyncio
async def test_instagram_adapter_sends_carousel_with_postback_payloads(conv):
    """carousel_items builds a Generic Template with one postback (payload=SKU) per card."""
    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_generic_template = AsyncMock(return_value=True)
        mock_outbound.ig_send_dm = AsyncMock()

        result = PipelineResult(
            text="Please reply with the number of your choice:\n1. ...\n2. ...",
            carousel_items=[
                CarouselItemSpec(sku="PR17761", title="Traditional Choli", subtitle="₹3,999",
                                  image_url="https://media.example.com/1/1/a.jpg"),
                CarouselItemSpec(sku="LH10042", title="Designer Lehenga", subtitle="₹6,500"),
            ],
        )
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    mock_outbound.ig_send_dm.assert_not_called()
    mock_outbound.ig_send_generic_template.assert_called_once()
    elements = mock_outbound.ig_send_generic_template.call_args.args[2]
    assert len(elements) == 2
    assert elements[0]["title"] == "Traditional Choli"
    assert elements[0]["image_url"] == "https://media.example.com/1/1/a.jpg"
    assert elements[0]["buttons"] == [{"type": "postback", "title": "Select", "payload": "PR17761"}]
    # No image_url on the second product — key is simply omitted, not sent as None.
    assert "image_url" not in elements[1]


@pytest.mark.asyncio
async def test_instagram_adapter_carousel_falls_back_to_text_on_send_failure(conv):
    """A failed/rejected carousel send falls back to the existing numbered-text reply."""
    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_generic_template = AsyncMock(return_value=False)
        mock_outbound.ig_send_dm = AsyncMock()

        result = PipelineResult(
            text="Please reply with the number of your choice:\n1. ...\n2. ...",
            carousel_items=[
                CarouselItemSpec(sku="PR17761", title="Traditional Choli", subtitle="₹3,999"),
                CarouselItemSpec(sku="LH10042", title="Designer Lehenga", subtitle="₹6,500"),
            ],
        )
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    mock_outbound.ig_send_generic_template.assert_called_once()
    mock_outbound.ig_send_dm.assert_called_once_with(
        "ig1", "rcpt1", result.text,
        kind=mock_outbound.ig_send_dm.call_args.kwargs["kind"],
        db=None, client_id=1, conversation_id=1,
    )


@pytest.mark.asyncio
async def test_instagram_adapter_carousel_caps_at_ten_cards(conv):
    """Only the first 10 carousel_items are sent, matching Meta's Generic Template limit."""
    with patch("app.routers._instagram_adapter.outbound") as mock_outbound:
        mock_outbound.ig_send_generic_template = AsyncMock(return_value=True)

        result = PipelineResult(
            text="choose one",
            carousel_items=[
                CarouselItemSpec(sku=f"SK{i:03d}", title=f"Product {i}", subtitle="₹100")
                for i in range(12)
            ],
        )
        await _instagram_adapter.send_pipeline_result(
            result, ig_user_id="ig1", recipient_igsid="rcpt1", db=None, conv=conv,
        )

    elements = mock_outbound.ig_send_generic_template.call_args.args[2]
    assert len(elements) == 10
