"""
Tests for app/services/order_service.py, focused on mark_order_paid and the
new invoice-generation-and-send behavior it triggers.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.order import Order
from app.services import order_service


def _make_order(**overrides) -> Order:
    defaults = dict(
        id=1, order_number="ORD-2026-0001", client_id=7, conversation_id=99,
        customer_name="Priya Sharma", customer_phone="919999999999",
        delivery_address="123 MG Road, Bengaluru",
        product_id=None,  # short-circuits _deduct_product_stock's DB query
        product_name="Silk Saree", product_sku="SKU001",
        variant_color="Blue", variant_size="M",
        quantity=1, unit_price=1500.0, total_amount=1500.0,
        payment_method="COD", payment_status="pending",
        status="confirmed", stock_deducted=False,
        created_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Order(**defaults)


def _make_client(**overrides) -> Client:
    defaults = dict(
        id=7, email="owner@biz.com", hashed_password="h",
        business_name="Riya Sarees", phone=None,  # skip owner WhatsApp notify
        logo_url=None,
    )
    defaults.update(overrides)
    return Client(**defaults)


def _conv_result(channel: str | None):
    result = MagicMock()
    if channel is None:
        result.scalar_one_or_none.return_value = None
    else:
        result.scalar_one_or_none.return_value = Conversation(id=99, client_id=7, channel=channel, phone_number="x")
    return result


async def test_mark_order_paid_already_deducted_is_idempotent(mock_db):
    """mark_order_paid returns False and does nothing when stock_deducted is already True."""
    order = _make_order(stock_deducted=True)
    client = _make_client()

    result = await order_service.mark_order_paid(mock_db, order, client)

    assert result is False
    mock_db.commit.assert_not_called()


async def test_mark_order_paid_generates_and_sends_invoice_on_whatsapp(mock_db):
    """A whatsapp-channel order gets an invoice generated, saved, and sent as a document."""
    order = _make_order()
    client = _make_client()
    mock_db.execute.return_value = _conv_result("whatsapp")

    with patch.object(
        order_service, "_deduct_product_stock", new=AsyncMock()
    ), patch(
        "app.services.invoice_service.generate_invoice_number", new=AsyncMock(return_value="INV-7-0001")
    ), patch(
        "app.services.invoice_service.load_client_logo_bytes", new=AsyncMock(return_value=None)
    ), patch(
        "app.services.invoice_service.generate_order_invoice", return_value=b"%PDF-fake"
    ), patch(
        "app.services.invoice_service.save_order_invoice_pdf",
        return_value="https://app.example.com/invoices/order_invoice_1.pdf",
    ), patch(
        "app.services.whatsapp_service.send_document_message", new=AsyncMock()
    ) as mock_send_doc:
        result = await order_service.mark_order_paid(mock_db, order, client)

    assert result is True
    assert order.invoice_number == "INV-7-0001"
    assert order.invoice_url == "https://app.example.com/invoices/order_invoice_1.pdf"
    mock_send_doc.assert_called_once()
    call_kwargs = mock_send_doc.call_args.kwargs
    assert call_kwargs["to_phone_number"] == "919999999999"
    assert call_kwargs["document_url"] == "https://app.example.com/invoices/order_invoice_1.pdf"
    assert call_kwargs["filename"] == "Invoice-INV-7-0001.pdf"


async def test_mark_order_paid_skips_document_send_on_non_whatsapp_channel(mock_db):
    """An Instagram-channel order still gets an invoice generated, but no WhatsApp document is sent."""
    order = _make_order()
    client = _make_client()
    mock_db.execute.return_value = _conv_result("instagram")

    with patch.object(
        order_service, "_deduct_product_stock", new=AsyncMock()
    ), patch(
        "app.services.invoice_service.generate_invoice_number", new=AsyncMock(return_value="INV-7-0001")
    ), patch(
        "app.services.invoice_service.load_client_logo_bytes", new=AsyncMock(return_value=None)
    ), patch(
        "app.services.invoice_service.generate_order_invoice", return_value=b"%PDF-fake"
    ), patch(
        "app.services.invoice_service.save_order_invoice_pdf",
        return_value="https://app.example.com/invoices/order_invoice_1.pdf",
    ), patch(
        "app.services.whatsapp_service.send_document_message", new=AsyncMock()
    ) as mock_send_doc:
        result = await order_service.mark_order_paid(mock_db, order, client)

    assert result is True
    assert order.invoice_number == "INV-7-0001"  # still generated/stored
    mock_send_doc.assert_not_called()


async def test_mark_order_paid_survives_invoice_generation_failure(mock_db):
    """An invoice-generation exception is swallowed — the paid transition still succeeds."""
    order = _make_order()
    client = _make_client()

    with patch.object(
        order_service, "_deduct_product_stock", new=AsyncMock()
    ), patch(
        "app.services.invoice_service.generate_invoice_number",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await order_service.mark_order_paid(mock_db, order, client)

    assert result is True
    assert order.status == "paid"
    assert order.stock_deducted is True
    assert order.invoice_number is None


async def test_mark_order_paid_no_conversation_skips_document_send(mock_db):
    """An order with no linked conversation_id still generates an invoice, skips the send."""
    order = _make_order(conversation_id=None)
    client = _make_client()

    with patch.object(
        order_service, "_deduct_product_stock", new=AsyncMock()
    ), patch(
        "app.services.invoice_service.generate_invoice_number", new=AsyncMock(return_value="INV-7-0001")
    ), patch(
        "app.services.invoice_service.load_client_logo_bytes", new=AsyncMock(return_value=None)
    ), patch(
        "app.services.invoice_service.generate_order_invoice", return_value=b"%PDF-fake"
    ), patch(
        "app.services.invoice_service.save_order_invoice_pdf",
        return_value="https://app.example.com/invoices/order_invoice_1.pdf",
    ), patch(
        "app.services.whatsapp_service.send_document_message", new=AsyncMock()
    ) as mock_send_doc:
        result = await order_service.mark_order_paid(mock_db, order, client)

    assert result is True
    mock_db.execute.assert_not_called()  # no Conversation lookup without conversation_id
    mock_send_doc.assert_not_called()
