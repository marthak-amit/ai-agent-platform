"""
Tests for app/services/catalogue_service.py and app/routers/catalogue.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.product import Product
from app.services import catalogue_service


# ── catalogue_service unit tests ─────────────────────────────────────────────

def _make_product(**kwargs) -> Product:
    """Build a Product ORM instance with sensible defaults."""
    defaults = dict(
        id=1, client_id=1, name="Test Product", price=100.0,
        stock=10, description="A test product", image_url=None,
        # New fields added in migration 0008 — must be set explicitly since
        # SQLAlchemy column defaults only fire on DB insert, not Python construction.
        is_active=True, low_stock_alert=5, sku=None, category=None,
        has_variants=False,
    )
    defaults.update(kwargs)
    return Product(**defaults)


# search_products

def test_search_products_matches_by_name():
    """Product with keyword in name gets a score and is returned."""
    products = [
        _make_product(id=1, name="Cotton Saree", description="Pure cotton"),
        _make_product(id=2, name="Silk Dupatta", description="Handwoven silk"),
    ]
    results = catalogue_service.search_products(products, "cotton saree")
    assert any(p.id == 1 for p in results)


def test_search_products_matches_by_description():
    """Product with keyword only in description is still returned."""
    products = [
        _make_product(id=1, name="Blue Fabric", description="handwoven cotton"),
        _make_product(id=2, name="Red Fabric", description="synthetic material"),
    ]
    results = catalogue_service.search_products(products, "handwoven")
    assert any(p.id == 1 for p in results)
    assert all(p.id != 2 for p in results)


def test_search_products_name_scores_higher_than_description():
    """Name match outranks description-only match."""
    products = [
        _make_product(id=1, name="Silk Saree", description="available in all sizes"),
        _make_product(id=2, name="Cotton Kurta", description="best silk quality"),
    ]
    results = catalogue_service.search_products(products, "silk")
    assert results[0].id == 1


def test_search_products_returns_top_5():
    """At most 5 products are returned."""
    products = [_make_product(id=i, name=f"Cotton Item {i}") for i in range(10)]
    results = catalogue_service.search_products(products, "cotton")
    assert len(results) <= 5


def test_search_products_no_match_returns_empty():
    """Specific query with no match returns empty list."""
    products = [_make_product(name="Silk Saree", description="pure silk")]
    results = catalogue_service.search_products(products, "leather shoes")
    assert results == []


def test_search_products_empty_query_returns_first_five():
    """Query with only stop words returns first top_k products."""
    products = [_make_product(id=i, name=f"Product {i}") for i in range(8)]
    results = catalogue_service.search_products(products, "what is the price")
    assert len(results) == 5


def test_search_products_empty_catalogue():
    """Empty catalogue returns empty list regardless of query."""
    assert catalogue_service.search_products([], "saree") == []


# format_catalogue_context

def test_format_catalogue_context_basic():
    """Context includes product name and price."""
    products = [_make_product(name="Cotton Saree", price=1500.0, stock=20, description="Premium cotton")]
    ctx = catalogue_service.format_catalogue_context(products)
    assert "Cotton Saree" in ctx
    assert "1,500" in ctx
    assert "20" in ctx
    assert "Premium cotton" in ctx


def test_format_catalogue_context_no_stock():
    """Products without stock omit the stock parenthetical."""
    products = [_make_product(name="Widget", price=99.0, stock=None, description=None)]
    ctx = catalogue_service.format_catalogue_context(products)
    assert "stock" not in ctx


def test_format_catalogue_context_empty_returns_empty_string():
    """Empty product list returns empty string."""
    assert catalogue_service.format_catalogue_context([]) == ""


def test_format_catalogue_context_multiple_products():
    """Context header appears exactly once."""
    products = [
        _make_product(id=1, name="Product A", price=100.0),
        _make_product(id=2, name="Product B", price=200.0),
    ]
    ctx = catalogue_service.format_catalogue_context(products)
    assert ctx.count("[Relevant products") == 1
    assert "Product A" in ctx
    assert "Product B" in ctx


# ── guard_product_reply ──────────────────────────────────────────────────────
#
# BUG: guard_product_reply's 0-relevance fallback echoed the customer's raw
# message back into a "we don't carry {query}" template — nonsensical when
# the query is bare smalltalk ("Hello") rather than a product-name claim,
# and separately, a SKU already resolved via a multi-choice pick this turn
# had no way to bypass the phantom check other than the caller perfectly
# rebuilding canonical_products. These tests characterize the fix directly
# against the pure function (no DB/webhook harness needed).

def test_guard_product_reply_greeting_query_does_not_echo_not_carry():
    """
    Customer sends a bare greeting ("Hello"); the LLM's open-browsing reply
    hallucinates a SKU/price not in the catalogue. The 0-relevance fallback
    must NOT echo "Hello" back as a fake product name ("we don't carry
    Hello") — it must fall back to the plain deterministic listing instead,
    same as the no-query case.
    """
    real_product = _make_product(
        id=1, name="Cotton Printed Saree", sku="SR10001", price=1200.0,
    )
    hallucinated_reply = (
        "Hi there! We have a lovely option:\n"
        "• Designer Georgette [SR99999] — ₹2500\n"
        "Would you like to order?"
    )
    result = catalogue_service.guard_product_reply(
        hallucinated_reply, [real_product], query="Hello",
    )
    assert "don't carry hello" not in result.lower(), (
        f"BUG: bare greeting query must never be echoed as a fake product "
        f"name in the not-carry fallback; got: {result!r}"
    )
    assert "SR99999" not in result, "Phantom SKU must still be stripped"
    assert "Cotton Printed Saree" in result, (
        f"Must fall back to the plain real-product listing; got: {result!r}"
    )


def test_guard_product_reply_pre_validated_sku_bypasses_phantom_check():
    """
    A SKU already resolved via a multi-choice pick this turn (passed as
    pre_validated_products) must never be flagged phantom, even when
    canonical_products is a stale/unrelated set — defense-in-depth on top
    of the caller rebuilding canonical_products after a pick resolves.
    """
    stale_unrelated_product = _make_product(
        id=1, name="Formal Shirt", sku="SR00100", price=699.0,
    )
    picked_product = _make_product(
        id=2, name="Cotton Saree", sku="SR33210", price=999.0,
    )
    pinned_fact_reply = (
        "Yes, we have this available:\n\n"
        "Cotton Saree [SR33210] — ₹999\n\n"
        "Would you like to order? (Yes / No)"
    )
    result = catalogue_service.guard_product_reply(
        pinned_fact_reply,
        [stale_unrelated_product],
        query="SR33210",
        pre_validated_products=[picked_product],
    )
    assert result == pinned_fact_reply, (
        f"Pre-validated pick's own SKU/price must bypass the phantom check "
        f"entirely, not be rejected as phantom against the stale unrelated "
        f"canonical set; got: {result!r}"
    )


def test_guard_product_reply_genuine_hallucinated_sku_still_caught():
    """
    A genuinely hallucinated SKU not in the catalogue and not pre-validated
    must still be caught and the reply replaced with a clean listing of the
    real product(s) — the legitimate phantom-SKU catch must keep working.
    """
    real_product = _make_product(
        id=1, name="Silk Saree", sku="SR10002", price=1500.0,
    )
    hallucinated_reply = (
        "Here are some options:\n"
        "• Silk Saree [SR10002] — ₹1500\n"
        "• Premium Saree [SR00000] — ₹5000\n"
        "Would you like to order?"
    )
    result = catalogue_service.guard_product_reply(
        hallucinated_reply, [real_product], query="saree",
    )
    assert "SR00000" not in result, f"Hallucinated SKU must be stripped: {result!r}"
    assert "5000" not in result, f"Hallucinated price must be stripped: {result!r}"
    assert "SR10002" in result, f"Real SKU must remain: {result!r}"


# ── router tests ──────────────────────────────────────────────────────────────

def test_add_product_returns_201(client, mock_db, mock_settings, make_test_user):
    """POST /catalogue/products creates a product and returns 201."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    from app.models.client import Client

    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    created_product = _make_product(
        id=10, client_id=1, name="Cotton Saree", price=1500.0, stock=20,
        description="Premium", image_url=None,
    )

    # First execute call: JWT client lookup; second: product refresh
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.create_product",
        new=AsyncMock(return_value=created_product),
    ):
        response = client.post(
            "/catalogue/products",
            json={"name": "Cotton Saree", "price": 1500.0, "stock": 20, "description": "Premium"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Cotton Saree"
    assert data["price"] == 1500.0


def test_add_product_requires_auth(client):
    """POST /catalogue/products returns 401 without a token."""
    response = client.post(
        "/catalogue/products",
        json={"name": "Item", "price": 100.0},
    )
    assert response.status_code == 401


def test_list_products_returns_200(client, mock_db, mock_settings, make_test_user):
    """GET /catalogue/products returns the client's product list."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result

    products = [
        _make_product(id=1, client_id=1, name="Saree", price=1000.0),
        _make_product(id=2, client_id=1, name="Kurta", price=500.0),
    ]

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.list_products",
        new=AsyncMock(return_value=products),
    ):
        response = client.get(
            "/catalogue/products",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_update_product_not_found_returns_404(client, mock_db, mock_settings, make_test_user):
    """PUT /catalogue/products/{id} returns 404 when product not found."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.get_product",
        new=AsyncMock(return_value=None),
    ):
        response = client.put(
            "/catalogue/products/999",
            json={"price": 200.0},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404


def test_delete_product_returns_204(client, mock_db, mock_settings, make_test_user):
    """DELETE /catalogue/products/{id} returns 204 on success."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result

    product = _make_product(id=5, client_id=1)

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.get_product",
        new=AsyncMock(return_value=product),
    ), __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.delete_product",
        new=AsyncMock(return_value=None),
    ):
        response = client.delete(
            "/catalogue/products/5",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204


def test_delete_product_not_found_returns_404(client, mock_db, mock_settings, make_test_user):
    """DELETE /catalogue/products/{id} returns 404 when product not found."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.catalogue_service.get_product",
        new=AsyncMock(return_value=None),
    ):
        response = client.delete(
            "/catalogue/products/99",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404


# ── upload-image endpoint ────────────────────────────────────────────────────

def _auth_headers(mock_db, make_test_user):
    """Build an Authorization header and wire mock_db to resolve the JWT client."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = make_test_user(existing_client)
    mock_db.execute.return_value = auth_result
    return {"Authorization": f"Bearer {token}"}


def test_upload_product_image_returns_url(client, mock_db, mock_settings, make_test_user):
    """POST /catalogue/products/upload-image stores the file and returns its URL."""
    headers = _auth_headers(mock_db, make_test_user)

    with patch(
        "app.routers.catalogue.storage_service.upload_product_image",
        return_value="https://media.example.com/1/42/abc123.jpg",
    ) as mock_upload:
        response = client.post(
            "/catalogue/products/upload-image",
            files={"file": ("photo.jpg", b"fake-image-bytes", "image/jpeg")},
            data={"product_id": "42"},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"url": "https://media.example.com/1/42/abc123.jpg"}
    mock_upload.assert_called_once_with(1, b"fake-image-bytes", 42)


def test_upload_product_image_rejects_disallowed_content_type(client, mock_db, mock_settings, make_test_user):
    """POST /catalogue/products/upload-image returns 400 for a non-image content type."""
    headers = _auth_headers(mock_db, make_test_user)

    response = client.post(
        "/catalogue/products/upload-image",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
        headers=headers,
    )

    assert response.status_code == 400
