"""
Razorpay payment service.

Creates UPI QR codes via the Razorpay REST API and verifies
incoming payment webhook signatures.
"""

import hashlib
import hmac

import httpx

from app.config import get_settings

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


async def create_qr_code(
    amount: int,
    description: str,
    phone_number: str = "",
) -> dict:
    """
    Create a Razorpay UPI QR code for a fixed payment amount.

    Args:
        amount:       Payment amount in paise (1 INR = 100 paise).
        description:  Human-readable payment description.
        phone_number: Customer identifier for record-keeping (not sent to Razorpay).

    Returns:
        Razorpay API response dict containing 'id', 'image_url', 'short_url', etc.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Razorpay API.
    """
    settings = get_settings()

    payload = {
        "type": "upi_qr",
        "name": description,
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount,
        "description": description,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{RAZORPAY_API_BASE}/payments/qr-codes",
            json=payload,
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        )
        response.raise_for_status()
        return response.json()


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Validate the X-Razorpay-Signature header on incoming payment webhooks.

    Razorpay signs the raw request body with HMAC-SHA256 using the webhook
    secret (RAZORPAY_KEY_SECRET).

    Args:
        payload_bytes:    Raw bytes of the POST request body.
        signature_header: Value of the X-Razorpay-Signature header.

    Returns:
        True if the signature is valid, False otherwise.
    """
    settings = get_settings()
    computed = hmac.new(
        key=settings.razorpay_key_secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature_header)
