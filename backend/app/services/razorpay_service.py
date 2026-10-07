"""
Razorpay payment service.

Creates UPI QR codes via the Razorpay REST API. (The old POST /payments/webhook
receiver and its signature check were removed; SellerTalk24 plan billing has its own
webhook at POST /billing/webhook — see app/services/billing/webhook.py.)
"""

import httpx

from app.config import get_settings

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


async def create_qr_code(
    amount: int,
    description: str,
    phone_number: str = "",
    key_id: str | None = None,
    key_secret: str | None = None,
) -> dict:
    """
    Create a Razorpay UPI QR code for a fixed payment amount.

    Args:
        amount:       Payment amount in paise (1 INR = 100 paise).
        description:  Human-readable payment description.
        phone_number: Customer identifier for record-keeping (not sent to Razorpay).
        key_id:       Per-client Razorpay key ID. Falls back to global env var.
        key_secret:   Per-client Razorpay key secret. Falls back to global env var.

    Returns:
        Razorpay API response dict containing 'id', 'image_url', 'short_url', etc.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Razorpay API.
    """
    settings = get_settings()
    _key_id = key_id or settings.razorpay_key_id
    _key_secret = key_secret or settings.razorpay_key_secret

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
            auth=(_key_id, _key_secret),
        )
        response.raise_for_status()
        return response.json()
