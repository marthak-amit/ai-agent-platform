"""
Tests for app/services/razorpay_service.py.
"""

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_razorpay_sig(body: bytes, secret: str = "test-rzp-secret") -> str:
    """Compute a valid Razorpay HMAC-SHA256 signature."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_create_qr_code_returns_data(mock_settings):
    """create_qr_code posts to Razorpay and returns parsed JSON."""
    from app.services.razorpay_service import create_qr_code

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "qr_abc123",
        "image_url": "https://rzp.io/qr/abc.png",
        "short_url": "https://rzp.io/l/abc",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.razorpay_service.httpx.AsyncClient", return_value=mock_client):
        result = await create_qr_code(amount=50000, description="Test payment")

    assert result["id"] == "qr_abc123"
    call_kwargs = mock_client.post.call_args[1]
    assert call_kwargs["auth"] == ("test-rzp-key", "test-rzp-secret")
    assert call_kwargs["json"]["payment_amount"] == 50000


def test_verify_webhook_signature_valid(mock_settings):
    """verify_webhook_signature returns True for correct HMAC."""
    from app.services.razorpay_service import verify_webhook_signature

    body = b'{"event":"payment.captured"}'
    sig = _make_razorpay_sig(body)
    assert verify_webhook_signature(body, sig) is True


def test_verify_webhook_signature_invalid(mock_settings):
    """verify_webhook_signature returns False for tampered body."""
    from app.services.razorpay_service import verify_webhook_signature

    body = b'{"event":"payment.captured"}'
    sig = _make_razorpay_sig(b'{"event":"other"}')
    assert verify_webhook_signature(body, sig) is False
