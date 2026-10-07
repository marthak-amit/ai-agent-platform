"""
Razorpay gateway client for SellerTalk24 prepaid billing (Orders API + Checkout.js).

Two interchangeable implementations behind one interface:

  * RazorpayClient      — wraps the official (synchronous) `razorpay` SDK; every network
                          call runs in a worker thread so route handlers stay async.
  * MockRazorpayClient  — no network. Fake ids, but signatures are REAL HMAC-SHA256
                          computed with fixed mock secrets, so the verify path is
                          genuinely exercised end to end.

`get_billing_gateway()` picks one from settings. Rules it enforces:
  * mock mode is on when SELLERTALK24_BILLING_MOCK says so, or automatically when the
    Razorpay key id/secret are empty;
  * mock mode is REFUSED when RAZORPAY_MODE=live (a missing key in production must fail
    loudly, never silently accept fake payments);
  * a key whose prefix contradicts RAZORPAY_MODE (rzp_live_ vs rzp_test_) is refused.

The mock is stateless on purpose (the app runs several workers): the amount is encoded
in the fake ids, so any worker can answer fetch_order / fetch_payment.

Secrets are never logged or included in repr()/exception text.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from functools import lru_cache
from typing import Any, Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

MIN_ORDER_PAISE = 100        # Razorpay minimum order amount (₹1)
MAX_RECEIPT_LENGTH = 40      # Razorpay receipt limit
MAX_NOTES = 15               # Razorpay notes limit

# Fixed, public, obviously-fake secrets: mock signatures are real HMACs, but only valid
# for the mock gateway. They protect nothing, so they must never be accepted in live mode.
MOCK_KEY_SECRET = "sellertalk24_mock_key_secret"
MOCK_WEBHOOK_SECRET = "sellertalk24_mock_webhook_secret"

_MOCK_ORDER_RE = re.compile(r"^order_mock_(\d+)_([0-9a-f]{8})$")
_MOCK_PAYMENT_RE = re.compile(r"^pay_mock_(\d+)_([0-9a-f]{8})_[0-9a-f]{6}$")


class BillingGatewayError(Exception):
    """A Razorpay (or mock) operation failed. The message never contains secrets."""


class BillingConfigError(BillingGatewayError):
    """Billing is misconfigured (e.g. mock mode in live mode, key/mode mismatch)."""


# --- signature primitives (shared by real + mock) ---------------------------------

def compute_payment_signature(secret: str, order_id: str, payment_id: str) -> str:
    """HMAC-SHA256 hex of ``"<order_id>|<payment_id>"`` — what Checkout.js hands back."""
    return hmac.new(
        secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def compute_webhook_signature(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 hex of the raw webhook body — the X-Razorpay-Signature value."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _constant_time_equal(expected: str, received: object) -> bool:
    """compare_digest that is False (not an exception) for None / non-str / non-ASCII input."""
    if not isinstance(received, str) or not received:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


def _validate_order_args(amount_paise: int, receipt: str, notes: dict | None) -> dict:
    """Validate create_order arguments against Razorpay's limits and return clean notes."""
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise ValueError("amount_paise must be an int (paise), never a float")
    if amount_paise < MIN_ORDER_PAISE:
        raise ValueError(f"amount_paise must be >= {MIN_ORDER_PAISE}")
    if not receipt or len(receipt) > MAX_RECEIPT_LENGTH:
        raise ValueError(f"receipt must be 1..{MAX_RECEIPT_LENGTH} characters")
    notes = dict(notes or {})
    if len(notes) > MAX_NOTES:
        raise ValueError(f"at most {MAX_NOTES} notes are allowed")
    return notes


def mode_for_key(key_id: str) -> str:
    """'live' for an rzp_live_ key, otherwise 'test' (rzp_test_ and anything unrecognised fail safe to test)."""
    return "live" if (key_id or "").startswith("rzp_live_") else "test"


def mode_for_settings(settings: Settings) -> str:
    """
    The billing mode these settings select: 'live' only when mock is off AND RAZORPAY_MODE=live; the mock gateway
    and test keys are 'test'. (get_billing_gateway refuses mock + live, so this never disagrees with gateway.mode.)
    """
    return "live" if (not settings.billing_mock_enabled and settings.razorpay_mode == "live") else "test"


class BillingGateway(Protocol):
    """The interface both the real and the mock client implement."""

    is_mock: bool
    mode: str   # 'test' | 'live' — stamped on every payment order; activation refuses a mismatch

    async def create_order(self, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
        """Create a Razorpay order for *amount_paise* and return the order dict (has ``id``)."""

    async def fetch_payment(self, payment_id: str) -> dict:
        """Fetch a payment (amount, status, order_id, ...) straight from the gateway."""

    async def fetch_order(self, order_id: str) -> dict:
        """Fetch an order (amount, amount_paid, status, ...) from the gateway."""

    async def fetch_order_payments(self, order_id: str) -> list[dict]:
        """All payment attempts made against an order (each like fetch_payment's dict). Used by reconciliation."""

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """True iff *signature* is the valid Checkout signature for (order_id, payment_id)."""

    def verify_webhook_signature(self, raw_body: bytes, header_sig: str) -> bool:
        """True iff *header_sig* is the valid webhook signature of the exact raw body."""


# --- real client ------------------------------------------------------------------

class RazorpayClient:
    """Real gateway: the official `razorpay` SDK, called off the event loop."""

    is_mock = False

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str = "") -> None:
        """Store credentials; the SDK client is created lazily on first network call."""
        if not key_id or not key_secret:
            raise BillingConfigError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required")
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self.mode = mode_for_key(key_id)
        self._sdk: Any = None

    def __repr__(self) -> str:
        """Debug form that never reveals the secrets."""
        return f"<RazorpayClient key_id={self._key_id[:8]}… webhook_secret_set={bool(self._webhook_secret)}>"

    def _sdk_client(self) -> Any:
        """Create (once) and return the official SDK client."""
        if self._sdk is None:
            import razorpay  # imported lazily: mock mode / tests don't need the SDK

            self._sdk = razorpay.Client(auth=(self._key_id, self._key_secret))
        return self._sdk

    async def _call(self, operation: str, func, *args) -> dict:
        """Run a blocking SDK call in a thread; map any failure to BillingGatewayError."""
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:  # SDK raises its own error types + requests errors
            logger.error("Razorpay %s failed: %s", operation, type(exc).__name__)
            raise BillingGatewayError(f"Razorpay {operation} failed ({type(exc).__name__}): {exc}") from exc

    async def create_order(self, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
        """Create an INR order for *amount_paise* with our unique *receipt*."""
        notes = _validate_order_args(amount_paise, receipt, notes)
        # payment_capture=1: capture on success. An uncaptured payment is auto-refunded by
        # Razorpay after ~5 days, which for prepaid billing would silently void a paid plan.
        payload = {
            "amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes,
            "payment_capture": 1,
        }
        sdk = self._sdk_client()
        return await self._call("create_order", sdk.order.create, payload)

    async def fetch_payment(self, payment_id: str) -> dict:
        """Fetch one payment by id."""
        sdk = self._sdk_client()
        return await self._call("fetch_payment", sdk.payment.fetch, payment_id)

    async def fetch_order(self, order_id: str) -> dict:
        """Fetch one order by id."""
        sdk = self._sdk_client()
        return await self._call("fetch_order", sdk.order.fetch, order_id)

    async def fetch_order_payments(self, order_id: str) -> list[dict]:
        """List the payment attempts of one order (Razorpay: GET /orders/{id}/payments)."""
        sdk = self._sdk_client()
        result = await self._call("fetch_order_payments", sdk.order.payments, order_id)
        return list((result or {}).get("items") or [])

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify the Checkout signature with the API key secret (constant-time)."""
        expected = compute_payment_signature(self._key_secret, order_id, payment_id)
        return _constant_time_equal(expected, signature)

    def verify_webhook_signature(self, raw_body: bytes, header_sig: str) -> bool:
        """Verify X-Razorpay-Signature with the webhook secret; fails closed if none is set."""
        if not self._webhook_secret:
            logger.error("RAZORPAY_WEBHOOK_SECRET is not set — webhook signature rejected.")
            return False
        expected = compute_webhook_signature(self._webhook_secret, raw_body)
        return _constant_time_equal(expected, header_sig)


# --- mock client ------------------------------------------------------------------

class MockRazorpayClient:
    """
    Offline gateway for development and tests. Same interface as RazorpayClient.

    Order id   ``order_mock_<amount>_<8hex>``
    Payment id ``pay_mock_<amount>_<order 8hex>_<6hex>``
    The amount lives in the id, so no storage is needed and any worker can answer.
    Limits (by design): fetch_order always reports status "created" and no receipt/notes —
    the DB row is the source of truth for those in mock mode.
    """

    is_mock = True
    mode = "test"

    def __repr__(self) -> str:
        """Debug form."""
        return "<MockRazorpayClient>"

    async def create_order(self, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
        """Return a Razorpay-shaped order dict with a fake id."""
        notes = _validate_order_args(amount_paise, receipt, notes)
        return {
            "id": f"order_mock_{amount_paise}_{secrets.token_hex(4)}",
            "entity": "order",
            "amount": amount_paise,
            "amount_paid": 0,
            "amount_due": amount_paise,
            "currency": "INR",
            "receipt": receipt,
            "status": "created",
            "attempts": 0,
            "notes": notes,
            "created_at": int(time.time()),
        }

    async def fetch_order(self, order_id: str) -> dict:
        """Return the order encoded in a mock order id."""
        match = _MOCK_ORDER_RE.match(order_id or "")
        if not match:
            raise BillingGatewayError(f"mock order not found: {order_id!r}")
        amount = int(match.group(1))
        return {
            "id": order_id, "entity": "order", "amount": amount, "amount_paid": 0,
            "amount_due": amount, "currency": "INR", "receipt": None, "status": "created",
            "attempts": 0, "notes": {},
        }

    async def fetch_payment(self, payment_id: str) -> dict:
        """Return the captured payment encoded in a mock payment id."""
        match = _MOCK_PAYMENT_RE.match(payment_id or "")
        if not match:
            raise BillingGatewayError(f"mock payment not found: {payment_id!r}")
        amount, order_token = int(match.group(1)), match.group(2)
        return {
            "id": payment_id, "entity": "payment", "amount": amount, "currency": "INR",
            "status": "captured", "captured": True, "order_id": f"order_mock_{amount}_{order_token}",
            "method": "mock", "notes": {},
        }

    async def fetch_order_payments(self, order_id: str) -> list[dict]:
        """The stateless mock never knows about payments made elsewhere: always none (tests subclass this)."""
        if not _MOCK_ORDER_RE.match(order_id or ""):
            raise BillingGatewayError(f"mock order not found: {order_id!r}")
        return []

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify with the fixed mock secret (a real HMAC check)."""
        expected = compute_payment_signature(MOCK_KEY_SECRET, order_id, payment_id)
        return _constant_time_equal(expected, signature)

    def verify_webhook_signature(self, raw_body: bytes, header_sig: str) -> bool:
        """Verify with the fixed mock webhook secret (a real HMAC check)."""
        return _constant_time_equal(compute_webhook_signature(MOCK_WEBHOOK_SECRET, raw_body), header_sig)

    # -- helpers used by /billing/mock/complete and the e2e script (mock only) --

    def simulate_payment(self, order_id: str) -> dict:
        """
        Pretend the customer paid *order_id*: return its fake payment id and the valid
        Checkout signature, exactly what Checkout.js would give the frontend.
        """
        match = _MOCK_ORDER_RE.match(order_id or "")
        if not match:
            raise BillingGatewayError(f"mock order not found: {order_id!r}")
        payment_id = f"pay_mock_{match.group(1)}_{match.group(2)}_{secrets.token_hex(3)}"
        return {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": compute_payment_signature(MOCK_KEY_SECRET, order_id, payment_id),
        }

    def sign_webhook(self, raw_body: bytes) -> str:
        """Return the valid X-Razorpay-Signature for *raw_body* (to post a mock webhook)."""
        return compute_webhook_signature(MOCK_WEBHOOK_SECRET, raw_body)


# --- factory ----------------------------------------------------------------------

@lru_cache(maxsize=4)
def _real_client(key_id: str, key_secret: str, webhook_secret: str) -> RazorpayClient:
    """One real client (and SDK session) per credential set."""
    return RazorpayClient(key_id, key_secret, webhook_secret)


def _check_key_matches_mode(key_id: str, mode: str) -> None:
    """Refuse an rzp_live_/rzp_test_ key that contradicts RAZORPAY_MODE."""
    prefix = "rzp_live_" if mode == "live" else "rzp_test_"
    if key_id.startswith(("rzp_live_", "rzp_test_")) and not key_id.startswith(prefix):
        raise BillingConfigError(
            f"RAZORPAY_MODE={mode} but RAZORPAY_KEY_ID has the other mode's prefix — refusing to start billing."
        )


_mock_warned = False


def _warn_mock_once() -> None:
    """Log the mock-mode warning once per process, not once per request."""
    global _mock_warned
    if not _mock_warned:
        _mock_warned = True
        logger.warning("SellerTalk24 billing is running in MOCK mode — no real payments are possible.")


def get_billing_gateway(settings: Settings | None = None) -> BillingGateway:
    """
    Return the gateway to use: mock or real, per the rules in the module docstring.

    Raises:
        BillingConfigError: mock mode requested/implied while RAZORPAY_MODE=live, or a
                            key/mode prefix mismatch.
    """
    settings = settings or get_settings()
    if settings.billing_mock_enabled:
        if settings.razorpay_mode == "live":
            raise BillingConfigError(
                "Billing mock mode is not allowed when RAZORPAY_MODE=live. "
                "Set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET (and unset SELLERTALK24_BILLING_MOCK)."
            )
        _warn_mock_once()
        return MockRazorpayClient()

    _check_key_matches_mode(settings.razorpay_key_id, settings.razorpay_mode)
    return _real_client(
        settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret
    )
