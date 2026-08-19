"""
Tests for app/services/order_pipeline.py::decide_send_instruction — the
Instagram carousel branch added for multi-product-match replies (mirrors the
existing WhatsApp choice_buttons/choice_list branch, but Instagram gets an
image-bearing Generic Template carousel instead, since WhatsApp's plain
button/list message type can't carry a per-row image without a Meta
Commerce catalog).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.product import Product
from app.services.order_pipeline import decide_send_instruction


def _product(sku: str, name: str, price: float, image_url: str | None = None) -> Product:
    return Product(
        id=hash(sku) % 10000, client_id=1, name=name, price=price, sku=sku,
        stock=10, is_active=True, has_variants=False, image_url=image_url,
    )


def _conv(pending_choice_skus: list[str]):
    conv = MagicMock()
    conv.id = 1
    conv.pending_choice_skus = json.dumps(pending_choice_skus)
    conv.pending_product_sku = None
    conv.pending_order_quantity = None
    return conv


async def test_instagram_multi_match_builds_carousel_items():
    """2+ matched SKUs on Instagram become carousel_items, not buttons/list_options."""
    conv = _conv(["PR17761", "LH10042"])
    client = MagicMock(id=1, accepts_cod=True)
    products = {
        "PR17761": _product("PR17761", "Traditional Choli", 3999.0, "https://media.example.com/1/1/a.jpg"),
        "LH10042": _product("LH10042", "Designer Lehenga", 6500.0, None),
    }

    with patch(
        "app.services.order_pipeline.catalogue_service.find_product_by_sku",
        new=AsyncMock(side_effect=lambda db, cid, sku: products.get(sku)),
    ):
        result = await decide_send_instruction(
            db=None, conv=conv, client=client, stage="browsing",
            ai_reply="Please reply with the number of your choice:\n1. ...\n2. ...",
            next_slot=None, is_whatsapp=False, pinned_product=None,
        )

    assert result.buttons is None
    assert result.list_options is None
    assert result.carousel_items is not None
    assert [c.sku for c in result.carousel_items] == ["PR17761", "LH10042"]
    assert result.carousel_items[0].title == "Traditional Choli"
    assert result.carousel_items[0].subtitle == "₹3,999"
    assert result.carousel_items[0].image_url == "https://media.example.com/1/1/a.jpg"
    # No image_url on the product — card simply omits it (None), no error.
    assert result.carousel_items[1].image_url is None


async def test_instagram_carousel_caps_at_ten_items():
    """More than 10 matched SKUs are truncated to the top 10 (Meta's carousel limit)."""
    skus = [f"SK{i:05d}" for i in range(12)]
    conv = _conv(skus)
    client = MagicMock(id=1, accepts_cod=True)
    products = {sku: _product(sku, f"Product {sku}", 500.0) for sku in skus}

    with patch(
        "app.services.order_pipeline.catalogue_service.find_product_by_sku",
        new=AsyncMock(side_effect=lambda db, cid, sku: products.get(sku)),
    ):
        result = await decide_send_instruction(
            db=None, conv=conv, client=client, stage="browsing",
            ai_reply="Please reply with the number of your choice:",
            next_slot=None, is_whatsapp=False, pinned_product=None,
        )

    assert len(result.carousel_items) == 10


async def test_whatsapp_multi_match_still_uses_buttons_not_carousel():
    """WhatsApp is unaffected by the new branch — still gets choice_buttons."""
    conv = _conv(["PR17761", "LH10042"])
    client = MagicMock(id=1, accepts_cod=True)
    products = {
        "PR17761": _product("PR17761", "Traditional Choli", 3999.0),
        "LH10042": _product("LH10042", "Designer Lehenga", 6500.0),
    }

    with patch(
        "app.services.order_pipeline.catalogue_service.find_product_by_sku",
        new=AsyncMock(side_effect=lambda db, cid, sku: products.get(sku)),
    ):
        result = await decide_send_instruction(
            db=None, conv=conv, client=client, stage="browsing",
            ai_reply="Please reply with the number of your choice:\n1. ...\n2. ...",
            next_slot=None, is_whatsapp=True, pinned_product=None,
        )

    assert result.carousel_items is None
    assert result.buttons is not None
    assert {b.id for b in result.buttons} == {"PR17761", "LH10042"}


async def test_no_pending_choice_skus_returns_plain_text_on_instagram():
    """With nothing to choose from, Instagram gets plain text — no empty carousel."""
    conv = _conv([])
    conv.pending_choice_skus = None
    client = MagicMock(id=1, accepts_cod=True)

    result = await decide_send_instruction(
        db=None, conv=conv, client=client, stage="browsing",
        ai_reply="Just a normal reply.",
        next_slot=None, is_whatsapp=False, pinned_product=None,
    )

    assert result.carousel_items is None
    assert result.text == "Just a normal reply."
