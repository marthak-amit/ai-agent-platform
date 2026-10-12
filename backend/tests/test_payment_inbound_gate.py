"""Tests for the 'is this image payment proof?' gate in payment_inbound."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import ocr_service, payment_inbound, vision_service


def _ctx(caption=None, media_url="https://x/y.jpg"):
    """Minimal pipeline ctx carrying one inbound image."""
    return SimpleNamespace(
        message=SimpleNamespace(type="image", image=SimpleNamespace(id="m1", caption=caption)),
        conv=SimpleNamespace(id=1),
        media_url=media_url,
        download_media=AsyncMock(return_value=b"img"),
    )


@pytest.fixture(autouse=True)
def _no_ocr_no_vision(monkeypatch):
    """Default: no watermark found, vision unavailable."""
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: None)
    monkeypatch.setattr(vision_service, "looks_like_product_photo", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_question_caption_is_not_proof():
    """'available?' on an image is a product enquiry, not a payment screenshot."""
    assert await payment_inbound._image_is_payment_proof(_ctx("available?")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", ["paid", "payment done", "screenshot", "UTR 1234"])
async def test_payment_caption_is_proof(caption):
    """A caption pointing at the payment stays proof."""
    assert await payment_inbound._image_is_payment_proof(_ctx(caption)) is True


@pytest.mark.asyncio
async def test_bare_image_defaults_to_proof():
    """No caption, no watermark, vision unavailable → keep legacy behaviour (proof)."""
    assert await payment_inbound._image_is_payment_proof(_ctx()) is True


@pytest.mark.asyncio
async def test_sku_watermark_means_product_photo(monkeypatch):
    """Our own SKU watermark on the image → product photo, not proof."""
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: "KU76326")
    assert await payment_inbound._image_is_payment_proof(_ctx()) is False


@pytest.mark.asyncio
async def test_vision_product_verdict_is_not_proof(monkeypatch):
    """Vision calling it a product photo → not proof."""
    monkeypatch.setattr(vision_service, "looks_like_product_photo", AsyncMock(return_value=True))
    assert await payment_inbound._image_is_payment_proof(_ctx()) is False


@pytest.mark.asyncio
async def test_vision_payment_verdict_stays_proof(monkeypatch):
    """Vision calling it a payment screenshot → proof."""
    monkeypatch.setattr(vision_service, "looks_like_product_photo", AsyncMock(return_value=False))
    assert await payment_inbound._image_is_payment_proof(_ctx()) is True


@pytest.mark.asyncio
async def test_looks_like_product_photo_none_when_vision_off(monkeypatch):
    """With vision disabled the classifier abstains."""
    from app.services import llm_health
    monkeypatch.setattr(llm_health, "is_vision_enabled", lambda: False)
    assert await vision_service.looks_like_product_photo(b"x") is None
