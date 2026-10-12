"""Every outgoing product photo must carry the SKU watermark (order_pipeline.watermarked_product_image_url)."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from app.services import order_pipeline as op

CLIENT = SimpleNamespace(id=7)
PRODUCT = SimpleNamespace(sku="PR10983", image_url="https://cdn.example.com/chair.jpg")


def _run(coro):
    """Run a coroutine to completion."""
    return asyncio.run(coro)


def test_returns_watermarked_url(monkeypatch):
    """The watermarked copy is what gets sent when watermarking works."""
    async def _wm(client_id, sku, url):
        """Fake watermark service."""
        assert (client_id, sku, url) == (7, "PR10983", PRODUCT.image_url)
        return "https://media.example.com/7/watermarked/PR10983.jpg"

    monkeypatch.setattr(op.storage_service, "get_watermarked_image_url", _wm)
    assert _run(op.watermarked_product_image_url(CLIENT, PRODUCT)).endswith("/watermarked/PR10983.jpg")


def test_falls_back_to_original_when_watermark_fails(monkeypatch):
    """No watermarked copy (fetch failed / error) → original URL, never an exception."""
    async def _none(*_a):
        """Watermarking unavailable."""
        return None

    async def _boom(*_a):
        """Watermarking raises."""
        raise RuntimeError("r2 down")

    monkeypatch.setattr(op.storage_service, "get_watermarked_image_url", _none)
    assert _run(op.watermarked_product_image_url(CLIENT, PRODUCT)) == PRODUCT.image_url
    monkeypatch.setattr(op.storage_service, "get_watermarked_image_url", _boom)
    assert _run(op.watermarked_product_image_url(CLIENT, PRODUCT)) == PRODUCT.image_url


def test_returns_none_without_a_usable_image():
    """Missing / non-http image → None so callers skip the send."""
    assert _run(op.watermarked_product_image_url(CLIENT, SimpleNamespace(sku="A", image_url=None))) is None
    assert _run(op.watermarked_product_image_url(CLIENT, SimpleNamespace(sku="A", image_url="/uploads/x.jpg"))) is None


def test_no_send_site_uses_a_raw_product_image_url():
    """Guard: queued product images/carousel cards must come from the watermark helper, not `.image_url`."""
    root = Path(op.__file__).parent
    pattern = re.compile(r"(images?\.append|pre_images\s*=|\"image_url\":)[^\n]*\.image_url|getattr\([^)]*\"image_url\"[^)]*\)\s*,?\s*\n?\s*\}\)")
    for name in ("order_pipeline.py", "router_actions.py"):
        text = (root / name).read_text()
        assert not pattern.search(text), f"raw product image_url queued for sending in {name}"
