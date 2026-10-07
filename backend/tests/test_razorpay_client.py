"""
Unit tests for app/services/billing/razorpay_client.py: signature verification (valid,
tampered, wrong secret), create/fetch wrappers (SDK stubbed), the mock gateway and the factory.
"""

import hashlib
import hmac
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.billing import razorpay_client as rc
from app.services.billing.razorpay_client import (
    MOCK_KEY_SECRET,
    BillingConfigError,
    BillingGatewayError,
    MockRazorpayClient,
    RazorpayClient,
    compute_payment_signature,
    compute_webhook_signature,
    get_billing_gateway,
)

KEY_ID = "rzp_test_abc123456789"
KEY_SECRET = "super-secret-key-value"
WH_SECRET = "super-secret-webhook-value"
ORDER, PAY = "order_Abc123", "pay_Xyz789"


def _real(webhook_secret: str = WH_SECRET) -> RazorpayClient:
    """A real client with known credentials (the SDK is never constructed)."""
    return RazorpayClient(KEY_ID, KEY_SECRET, webhook_secret)


def _flip(sig: str) -> str:
    """Tamper with a hex signature: change its last character."""
    return sig[:-1] + ("0" if sig[-1] != "0" else "1")


def _settings(**kw) -> SimpleNamespace:
    """Settings-shaped object for the factory (mock mode derived like Settings does)."""
    base = dict(razorpay_key_id="", razorpay_key_secret="", razorpay_webhook_secret="", razorpay_mode="test")
    base.update(kw)
    explicit = base.pop("billing_mock", None)
    base["billing_mock_enabled"] = (
        explicit if explicit is not None else not (base["razorpay_key_id"] and base["razorpay_key_secret"])
    )
    return SimpleNamespace(**base)


# --- signature primitives ---------------------------------------------------------

def test_compute_payment_signature_matches_razorpay_documented_construction():
    expected = hmac.new(KEY_SECRET.encode(), f"{ORDER}|{PAY}".encode(), hashlib.sha256).hexdigest()
    assert compute_payment_signature(KEY_SECRET, ORDER, PAY) == expected


def test_compute_webhook_signature_is_hmac_of_raw_body():
    body = b'{"event":"payment.captured"}'
    assert compute_webhook_signature(WH_SECRET, body) == hmac.new(WH_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_payment_signature_agrees_with_the_official_sdk_utility():
    razorpay = pytest.importorskip("razorpay")
    sdk = razorpay.Client(auth=(KEY_ID, KEY_SECRET))
    sig = compute_payment_signature(KEY_SECRET, ORDER, PAY)
    # raises SignatureVerificationError if our construction ever diverges from the SDK's
    sdk.utility.verify_payment_signature(
        {"razorpay_order_id": ORDER, "razorpay_payment_id": PAY, "razorpay_signature": sig}
    )


def test_constant_time_equal_is_false_not_an_exception_for_bad_input():
    assert rc._constant_time_equal("abc", "abc") is True
    for bad in (None, "", 123, b"abc", "ünïcode", "abd"):
        assert rc._constant_time_equal("abc", bad) is False


# --- real client: payment signature -----------------------------------------------

def test_verify_payment_signature_accepts_the_valid_signature():
    sig = compute_payment_signature(KEY_SECRET, ORDER, PAY)
    assert _real().verify_payment_signature(ORDER, PAY, sig) is True


def test_verify_payment_signature_rejects_a_tampered_signature():
    sig = compute_payment_signature(KEY_SECRET, ORDER, PAY)
    assert _real().verify_payment_signature(ORDER, PAY, _flip(sig)) is False


def test_verify_payment_signature_rejects_a_tampered_order_or_payment_id():
    sig = compute_payment_signature(KEY_SECRET, ORDER, PAY)
    assert _real().verify_payment_signature("order_Other", PAY, sig) is False
    assert _real().verify_payment_signature(ORDER, "pay_Other", sig) is False


def test_verify_payment_signature_rejects_a_signature_made_with_the_wrong_secret():
    forged = compute_payment_signature("some-other-secret", ORDER, PAY)
    assert _real().verify_payment_signature(ORDER, PAY, forged) is False


def test_verify_payment_signature_rejects_the_webhook_secret_as_key():
    """A signature made with the webhook secret must not pass as a Checkout signature."""
    assert _real().verify_payment_signature(ORDER, PAY, compute_payment_signature(WH_SECRET, ORDER, PAY)) is False


@pytest.mark.parametrize("bad", [None, "", "not-hex", "ünï"])
def test_verify_payment_signature_rejects_garbage_without_raising(bad):
    assert _real().verify_payment_signature(ORDER, PAY, bad) is False


# --- real client: webhook signature -----------------------------------------------

BODY = b'{"event":"payment.captured","payload":{}}'


def test_verify_webhook_signature_accepts_the_valid_signature():
    assert _real().verify_webhook_signature(BODY, compute_webhook_signature(WH_SECRET, BODY)) is True


def test_verify_webhook_signature_rejects_a_tampered_body():
    sig = compute_webhook_signature(WH_SECRET, BODY)
    assert _real().verify_webhook_signature(BODY + b" ", sig) is False
    assert _real().verify_webhook_signature(BODY.replace(b"captured", b"failed_"), sig) is False


def test_verify_webhook_signature_rejects_a_tampered_signature():
    assert _real().verify_webhook_signature(BODY, _flip(compute_webhook_signature(WH_SECRET, BODY))) is False


def test_verify_webhook_signature_rejects_the_wrong_secret():
    assert _real().verify_webhook_signature(BODY, compute_webhook_signature("nope", BODY)) is False


def test_verify_webhook_signature_fails_closed_without_a_webhook_secret(caplog):
    client = _real(webhook_secret="")
    with caplog.at_level(logging.ERROR):
        # even the signature an empty-key HMAC would produce must be rejected
        assert client.verify_webhook_signature(BODY, compute_webhook_signature("", BODY)) is False
    assert "RAZORPAY_WEBHOOK_SECRET" in caplog.text


@pytest.mark.parametrize("bad", [None, "", "xyz"])
def test_verify_webhook_signature_rejects_missing_header(bad):
    assert _real().verify_webhook_signature(BODY, bad) is False


# --- real client: construction, secrecy -------------------------------------------

@pytest.mark.parametrize("key_id,key_secret", [("", KEY_SECRET), (KEY_ID, ""), ("", "")])
def test_real_client_requires_both_keys(key_id, key_secret):
    with pytest.raises(BillingConfigError):
        RazorpayClient(key_id, key_secret, WH_SECRET)


def test_repr_never_contains_secrets():
    text = repr(_real())
    assert KEY_SECRET not in text and WH_SECRET not in text and KEY_ID not in text  # id is truncated too


# --- real client: create_order / fetch (SDK stubbed) ------------------------------

def _stub_sdk(client: RazorpayClient) -> MagicMock:
    """Replace the SDK with a MagicMock so no network/SDK import happens."""
    sdk = MagicMock()
    client._sdk = sdk
    return sdk


@pytest.mark.asyncio
async def test_create_order_sends_inr_paise_receipt_notes_and_auto_capture():
    client = _real()
    sdk = _stub_sdk(client)
    sdk.order.create.return_value = {"id": "order_1", "amount": 542682}

    result = await client.create_order(542682, "st24_7_1700000000", {"client_id": "7"})

    assert result == {"id": "order_1", "amount": 542682}
    sdk.order.create.assert_called_once_with(
        {
            "amount": 542682, "currency": "INR", "receipt": "st24_7_1700000000",
            "notes": {"client_id": "7"}, "payment_capture": 1,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount,receipt,notes",
    [
        (542682.0, "r", None),                 # float
        (True, "r", None),                     # bool is an int subclass
        (99, "r", None),                       # below Razorpay minimum
        (0, "r", None),
        (100, "", None),                       # empty receipt
        (100, "x" * 41, None),                 # receipt too long
        (100, "r", {str(i): "v" for i in range(16)}),   # too many notes
    ],
)
async def test_create_order_validates_arguments_before_calling_razorpay(amount, receipt, notes):
    client = _real()
    sdk = _stub_sdk(client)
    with pytest.raises(ValueError):
        await client.create_order(amount, receipt, notes)
    sdk.order.create.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_payment_and_fetch_order_delegate_to_the_sdk():
    client = _real()
    sdk = _stub_sdk(client)
    sdk.payment.fetch.return_value = {"id": PAY, "amount": 100, "status": "captured"}
    sdk.order.fetch.return_value = {"id": ORDER, "status": "paid"}

    assert (await client.fetch_payment(PAY))["status"] == "captured"
    assert (await client.fetch_order(ORDER))["status"] == "paid"
    sdk.payment.fetch.assert_called_once_with(PAY)
    sdk.order.fetch.assert_called_once_with(ORDER)


@pytest.mark.asyncio
async def test_sdk_failure_becomes_billing_gateway_error_without_leaking_secrets(caplog):
    client = _real()
    sdk = _stub_sdk(client)
    sdk.order.create.side_effect = RuntimeError("Authentication failed")

    with caplog.at_level(logging.DEBUG), pytest.raises(BillingGatewayError) as err:
        await client.create_order(500, "r1")

    assert "create_order" in str(err.value) and "RuntimeError" in str(err.value)
    for text in (str(err.value), caplog.text):
        assert KEY_SECRET not in text and WH_SECRET not in text


def test_sdk_client_is_created_lazily_once(monkeypatch):
    created = []
    fake = SimpleNamespace(Client=lambda auth: created.append(auth) or "sdk")
    monkeypatch.setitem(__import__("sys").modules, "razorpay", fake)
    client = _real()
    assert created == []                       # nothing at construction
    assert client._sdk_client() == "sdk" and client._sdk_client() == "sdk"
    assert created == [(KEY_ID, KEY_SECRET)]   # built once, with the key pair


# --- mock client ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_create_order_returns_a_razorpay_shaped_order_with_a_fake_id():
    order = await MockRazorpayClient().create_order(542682, "st24_1_1", {"a": "b"})
    assert order["id"].startswith("order_mock_542682_")
    assert (order["amount"], order["amount_due"], order["currency"], order["status"]) == (542682, 542682, "INR", "created")
    assert order["receipt"] == "st24_1_1" and order["notes"] == {"a": "b"}


@pytest.mark.asyncio
async def test_mock_create_order_validates_like_the_real_client():
    with pytest.raises(ValueError):
        await MockRazorpayClient().create_order(50, "r")


@pytest.mark.asyncio
async def test_mock_order_ids_are_unique():
    mock = MockRazorpayClient()
    ids = {(await mock.create_order(100, "r"))["id"] for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.asyncio
async def test_mock_is_stateless_so_another_instance_can_answer_fetches():
    order = await MockRazorpayClient().create_order(542682, "r")
    other = MockRazorpayClient()          # e.g. a different gunicorn/uvicorn worker
    assert (await other.fetch_order(order["id"]))["amount"] == 542682
    paid = other.simulate_payment(order["id"])
    payment = await MockRazorpayClient().fetch_payment(paid["razorpay_payment_id"])
    assert payment["amount"] == 542682 and payment["order_id"] == order["id"]
    assert payment["status"] == "captured" and payment["captured"] is True


@pytest.mark.asyncio
async def test_mock_unknown_or_malformed_ids_raise_not_found():
    mock = MockRazorpayClient()
    for bad in ("order_real_1", "", None, "order_mock_x_1"):
        with pytest.raises(BillingGatewayError):
            await mock.fetch_order(bad)
    for bad in ("pay_1", "", None, "pay_mock_100_zz_1"):
        with pytest.raises(BillingGatewayError):
            await mock.fetch_payment(bad)
    with pytest.raises(BillingGatewayError):
        mock.simulate_payment("nope")


@pytest.mark.asyncio
async def test_mock_simulated_payment_passes_the_real_hmac_verification():
    mock = MockRazorpayClient()
    order = await mock.create_order(1000, "r")
    paid = mock.simulate_payment(order["id"])

    assert paid["razorpay_signature"] == compute_payment_signature(
        MOCK_KEY_SECRET, order["id"], paid["razorpay_payment_id"]
    )
    assert mock.verify_payment_signature(
        paid["razorpay_order_id"], paid["razorpay_payment_id"], paid["razorpay_signature"]
    ) is True


@pytest.mark.asyncio
async def test_mock_verification_rejects_tampering_and_other_secrets():
    mock = MockRazorpayClient()
    paid = mock.simulate_payment((await mock.create_order(1000, "r"))["id"])
    o, p, s = paid["razorpay_order_id"], paid["razorpay_payment_id"], paid["razorpay_signature"]

    assert mock.verify_payment_signature(o, p, _flip(s)) is False
    assert mock.verify_payment_signature(o, "pay_mock_1000_00000000_aaaaaa", s) is False
    assert mock.verify_payment_signature(o, p, compute_payment_signature("other", o, p)) is False
    assert mock.verify_payment_signature(o, p, None) is False
    # and a real client (real secret) rejects a mock signature
    assert _real().verify_payment_signature(o, p, s) is False


def test_mock_webhook_signing_round_trips_and_detects_tampering():
    mock = MockRazorpayClient()
    sig = mock.sign_webhook(BODY)
    assert mock.verify_webhook_signature(BODY, sig) is True
    assert mock.verify_webhook_signature(BODY + b"x", sig) is False
    assert mock.verify_webhook_signature(BODY, _flip(sig)) is False
    assert mock.verify_webhook_signature(BODY, None) is False
    assert _real().verify_webhook_signature(BODY, sig) is False


def test_mock_repr():
    assert repr(MockRazorpayClient()) == "<MockRazorpayClient>"


def test_both_clients_flag_themselves():
    assert MockRazorpayClient.is_mock is True and RazorpayClient.is_mock is False


# --- factory ----------------------------------------------------------------------

def test_factory_returns_mock_when_keys_are_empty():
    assert isinstance(get_billing_gateway(_settings()), MockRazorpayClient)


def test_factory_returns_mock_when_flag_is_true_even_with_keys():
    gateway = get_billing_gateway(_settings(razorpay_key_id=KEY_ID, razorpay_key_secret=KEY_SECRET, billing_mock=True))
    assert isinstance(gateway, MockRazorpayClient)


def test_factory_returns_the_real_client_when_keys_are_set():
    gateway = get_billing_gateway(
        _settings(razorpay_key_id=KEY_ID, razorpay_key_secret=KEY_SECRET, razorpay_webhook_secret=WH_SECRET)
    )
    assert isinstance(gateway, RazorpayClient) and gateway.is_mock is False


def test_factory_reuses_the_same_real_client_for_the_same_credentials():
    s = _settings(razorpay_key_id=KEY_ID, razorpay_key_secret=KEY_SECRET)
    assert get_billing_gateway(s) is get_billing_gateway(s)


def test_factory_flag_false_with_empty_keys_fails_instead_of_silently_mocking():
    with pytest.raises(BillingConfigError):
        get_billing_gateway(_settings(billing_mock=False))


def test_factory_refuses_mock_mode_in_live_mode():
    with pytest.raises(BillingConfigError, match="live"):
        get_billing_gateway(_settings(razorpay_mode="live"))                      # keys empty -> auto mock
    with pytest.raises(BillingConfigError, match="live"):
        get_billing_gateway(_settings(razorpay_mode="live", razorpay_key_id="rzp_live_x",
                                      razorpay_key_secret="s", billing_mock=True))  # explicit mock


@pytest.mark.parametrize(
    "mode,key_id", [("live", "rzp_test_abc"), ("test", "rzp_live_abc")]
)
def test_factory_refuses_a_key_that_contradicts_the_mode(mode, key_id):
    with pytest.raises(BillingConfigError, match="prefix"):
        get_billing_gateway(_settings(razorpay_mode=mode, razorpay_key_id=key_id, razorpay_key_secret="s"))


def test_factory_accepts_matching_live_key():
    gateway = get_billing_gateway(_settings(razorpay_mode="live", razorpay_key_id="rzp_live_abc", razorpay_key_secret="s"))
    assert isinstance(gateway, RazorpayClient)


def test_factory_warns_about_mock_mode_once_per_process(caplog, monkeypatch):
    monkeypatch.setattr(rc, "_mock_warned", False)
    with caplog.at_level(logging.WARNING):
        get_billing_gateway(_settings())
        get_billing_gateway(_settings())
    assert caplog.text.count("MOCK mode") == 1


def test_check_key_matches_mode_ignores_unprefixed_keys():
    rc._check_key_matches_mode("custom_key", "live")  # no exception


def test_validate_order_args_returns_a_copy_of_notes():
    notes = {"a": "b"}
    cleaned = rc._validate_order_args(100, "r", notes)
    assert cleaned == notes and cleaned is not notes
    assert rc._validate_order_args(100, "r", None) == {}


@pytest.mark.asyncio
async def test_real_sdk_receives_the_orders_api_call_and_the_fetches(monkeypatch):
    """Drive the real `razorpay` SDK object with its HTTP layer stubbed (skipped if not installed)."""
    razorpay = pytest.importorskip("razorpay")
    calls = []

    def fake_request(self, method, path, **kwargs):
        calls.append((method, path, kwargs.get("data")))
        return {"id": "order_live_1", "status": "created"}

    monkeypatch.setattr(razorpay.Client, "request", fake_request)
    client = _real()

    order = await client.create_order(542682, "st24_7_1", {"client_id": "7"})
    await client.fetch_order("order_live_1")
    await client.fetch_payment("pay_1")

    assert order["id"] == "order_live_1"
    method, path, body = calls[0]
    sent = json.loads(body)  # the SDK sends the body as a JSON string
    assert method == "post" and path.endswith("/orders")
    assert sent == {
        "amount": 542682, "currency": "INR", "receipt": "st24_7_1",
        "notes": {"client_id": "7"}, "payment_capture": 1,
    }
    assert calls[1][0] == "get" and calls[1][1].endswith("/orders/order_live_1")
    assert calls[2][0] == "get" and calls[2][1].endswith("/payments/pay_1")
