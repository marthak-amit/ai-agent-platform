"""
Tests for the order-invoice functions in app/services/invoice_service.py.

generate_gst_invoice (the pre-existing Payment/Razorpay invoice function) is
untouched by this feature and has no test coverage today either — these
tests cover only the new Order-invoice additions.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models.client import Client
from app.models.order import Order
from app.services import invoice_service


def _make_order(**overrides) -> Order:
    defaults = dict(
        id=42, order_number="ORD-2026-0001", client_id=7,
        customer_name="Priya Sharma", customer_phone="919999999999",
        delivery_address="123 MG Road, Bengaluru",
        product_name="Silk Saree", product_sku="SKU001",
        variant_color="Blue", variant_size="M", variant_material=None,
        quantity=2, unit_price=1500.0, total_amount=3000.0,
        payment_method="COD", payment_status="paid",
        created_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Order(**defaults)


def _make_client(**overrides) -> Client:
    defaults = dict(
        id=7, email="owner@biz.com", hashed_password="h",
        business_name="Riya Sarees", business_address="45 Textile Market, Surat",
        gst_number="24ABCDE1234F1Z5", phone="918888888888",
        delivery_days_min=3, delivery_days_max=7,
    )
    defaults.update(overrides)
    return Client(**defaults)


# ── generate_invoice_number ────────────────────────────────────────────────────

async def test_generate_invoice_number_first_invoice():
    """First invoice for a client is INV-{client_id}-0001."""
    db = AsyncMock()
    result = MagicMock(); result.scalar_one.return_value = 0
    db.execute.return_value = result

    number = await invoice_service.generate_invoice_number(db, client_id=7)
    assert number == "INV-7-0001"


async def test_generate_invoice_number_increments():
    """The Nth invoice for a client is INV-{client_id}-{N+1}, zero-padded."""
    db = AsyncMock()
    result = MagicMock(); result.scalar_one.return_value = 3
    db.execute.return_value = result

    number = await invoice_service.generate_invoice_number(db, client_id=7)
    assert number == "INV-7-0004"


# ── load_client_logo_bytes ─────────────────────────────────────────────────────

async def test_load_client_logo_bytes_no_logo_returns_none():
    """A client with no logo_url returns None without any I/O."""
    client = _make_client(logo_url=None)
    assert await invoice_service.load_client_logo_bytes(client) is None


async def test_load_client_logo_bytes_local_path(tmp_path, monkeypatch):
    """A '/uploads/...' logo_url is read directly from local disk."""
    monkeypatch.setattr(invoice_service, "_UPLOADS_DIR", str(tmp_path))
    (tmp_path / "logo123.png").write_bytes(b"fake-png-bytes")

    client = _make_client(logo_url="/uploads/logo123.png")
    result = await invoice_service.load_client_logo_bytes(client)
    assert result == b"fake-png-bytes"


async def test_load_client_logo_bytes_local_path_missing_file_returns_none(tmp_path, monkeypatch):
    """A '/uploads/...' logo_url pointing at a nonexistent file returns None."""
    monkeypatch.setattr(invoice_service, "_UPLOADS_DIR", str(tmp_path))
    client = _make_client(logo_url="/uploads/does-not-exist.png")
    assert await invoice_service.load_client_logo_bytes(client) is None


async def test_load_client_logo_bytes_remote_url():
    """An http(s) logo_url is fetched via httpx."""
    client = _make_client(logo_url="https://cdn.example.com/logo.png")

    mock_response = MagicMock()
    mock_response.content = b"remote-logo-bytes"
    mock_response.raise_for_status = MagicMock()
    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.get = AsyncMock(return_value=mock_response)

    with patch("app.services.invoice_service.httpx.AsyncClient", return_value=mock_http_client):
        result = await invoice_service.load_client_logo_bytes(client)

    assert result == b"remote-logo-bytes"


async def test_load_client_logo_bytes_remote_fetch_failure_returns_none():
    """A failed remote logo fetch returns None instead of raising."""
    client = _make_client(logo_url="https://cdn.example.com/logo.png")

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))

    with patch("app.services.invoice_service.httpx.AsyncClient", return_value=mock_http_client):
        result = await invoice_service.load_client_logo_bytes(client)

    assert result is None


# ── generate_order_invoice ─────────────────────────────────────────────────────

def test_generate_order_invoice_produces_valid_pdf():
    """generate_order_invoice returns non-trivial, valid PDF bytes."""
    order = _make_order()
    client = _make_client()

    pdf_bytes = invoice_service.generate_order_invoice(order, client, "INV-7-0001")

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 500


def test_generate_order_invoice_with_logo_bytes():
    """Passing logo_bytes does not raise and still produces a valid PDF."""
    order = _make_order()
    client = _make_client()

    # Minimal valid 1x1 PNG.
    png_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    pdf_bytes = invoice_service.generate_order_invoice(order, client, "INV-7-0001", png_1x1)
    assert pdf_bytes.startswith(b"%PDF")


def test_generate_order_invoice_handles_missing_optional_fields():
    """Missing gst_number/business_address/variant fields don't raise."""
    order = _make_order(variant_color=None, variant_size=None, variant_material=None)
    client = _make_client(gst_number=None, business_address=None, phone=None)

    pdf_bytes = invoice_service.generate_order_invoice(order, client, "INV-7-0002")
    assert pdf_bytes.startswith(b"%PDF")


def test_generate_order_invoice_bad_logo_bytes_does_not_raise():
    """Corrupt logo_bytes fall back gracefully instead of crashing generation."""
    order = _make_order()
    client = _make_client()

    pdf_bytes = invoice_service.generate_order_invoice(order, client, "INV-7-0001", b"not-an-image")
    assert pdf_bytes.startswith(b"%PDF")


# ── save_order_invoice_pdf ─────────────────────────────────────────────────────

def test_save_order_invoice_pdf_writes_file_and_returns_url(tmp_path, monkeypatch):
    """save_order_invoice_pdf writes the PDF to disk and returns an absolute URL."""
    monkeypatch.setattr(invoice_service, "_INVOICES_DIR", str(tmp_path))

    settings = MagicMock(backend_public_url="https://app.example.com")
    with patch("app.services.invoice_service.get_settings", return_value=settings):
        url = invoice_service.save_order_invoice_pdf(42, b"%PDF-fake-content")

    assert url == "https://app.example.com/invoices/order_invoice_42.pdf"
    assert (tmp_path / "order_invoice_42.pdf").read_bytes() == b"%PDF-fake-content"
