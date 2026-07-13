"""
Tests for app.services.order_pipeline._find_cross_sell_candidate — the
browsed-SKU cross-sell lookup shared by the order summary (pre-payment) and
the paid/confirmed reply (order flow rule 3: catalog send on paid/confirmed).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.order_pipeline import _find_cross_sell_candidate


def _conv(browsed_skus=None):
    conv = MagicMock()
    conv.browsed_skus = json.dumps(browsed_skus) if browsed_skus is not None else None
    return conv


def _client(client_id=5):
    return SimpleNamespace(id=client_id)


@pytest.mark.asyncio
async def test_no_client_returns_none():
    """Without a client, no lookup is attempted."""
    result = await _find_cross_sell_candidate(AsyncMock(), None, _conv(["PR200"]), "PR100")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_no_browsed_skus_returns_none():
    """An empty/absent browsed_skus list yields no candidate."""
    result = await _find_cross_sell_candidate(AsyncMock(), _client(), _conv(None), "PR100")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_only_pinned_sku_browsed_returns_none():
    """If the only browsed SKU is the currently-pinned one, there's no candidate."""
    result = await _find_cross_sell_candidate(AsyncMock(), _client(), _conv(["PR100"]), "PR100")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_malformed_browsed_skus_treated_as_empty():
    """Invalid JSON in browsed_skus is swallowed, not raised."""
    conv = MagicMock()
    conv.browsed_skus = "{not valid json"
    result = await _find_cross_sell_candidate(AsyncMock(), _client(), conv, "PR100")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_returns_most_recent_different_sku():
    """The last browsed SKU that differs from pinned_sku is looked up and returned."""
    product = SimpleNamespace(name="Banarasi Dupatta", price=799)
    db = AsyncMock()

    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(return_value=product),
    ) as find_mock:
        name, price_fmt = await _find_cross_sell_candidate(
            db, _client(client_id=5), _conv(["PR100", "PR200"]), "PR100"
        )

    find_mock.assert_called_once_with(db, 5, "PR200")
    assert name == "Banarasi Dupatta"
    assert price_fmt == "₹799"


@pytest.mark.asyncio
async def test_product_lookup_failure_returns_none():
    """A lookup exception is caught and treated as no candidate."""
    db = AsyncMock()
    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        result = await _find_cross_sell_candidate(db, _client(), _conv(["PR100", "PR200"]), "PR100")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_product_not_found_returns_none():
    """When the SKU lookup returns nothing, there's no candidate."""
    db = AsyncMock()
    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(return_value=None),
    ):
        result = await _find_cross_sell_candidate(db, _client(), _conv(["PR100", "PR200"]), "PR100")
    assert result == (None, None)
