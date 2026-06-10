"""
Tests for app/services/catalogue_service.py and app/routers/catalogue.py.
"""

from unittest.mock import AsyncMock, MagicMock

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


# ── router tests ──────────────────────────────────────────────────────────────

def test_add_product_returns_201(client, mock_db, mock_settings):
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
    auth_result.scalar_one_or_none.return_value = existing_client
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


def test_list_products_returns_200(client, mock_db, mock_settings):
    """GET /catalogue/products returns the client's product list."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = existing_client
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


def test_update_product_not_found_returns_404(client, mock_db, mock_settings):
    """PUT /catalogue/products/{id} returns 404 when product not found."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = existing_client
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


def test_delete_product_returns_204(client, mock_db, mock_settings):
    """DELETE /catalogue/products/{id} returns 204 on success."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = existing_client
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


def test_delete_product_not_found_returns_404(client, mock_db, mock_settings):
    """DELETE /catalogue/products/{id} returns 404 when product not found."""
    from app.services.auth_service import create_access_token
    from app.models.client import Client

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(id=1, email="owner@biz.com", hashed_password="h", is_active=True)
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = existing_client
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
