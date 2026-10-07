"""Unit tests (no DB) for the small pure helpers behind the SellerTalk24 billing API."""

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.sellertalk24_billing import PaymentOrder
from app.routers.billing import _http, get_gateway
from app.services.billing import checkout as checkout_mod
from app.services.billing import subscriptions as subs
from app.services.billing import views
from app.services.billing import webhook as webhook_mod
from app.services.billing.activation import ActivationError, _check_payment
from app.services.billing.pricing import AmountBreakdown
from app.services.billing.razorpay_client import MockRazorpayClient, RazorpayClient

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _sub(**kw):
    """Subscription-shaped stand-in."""
    base = dict(
        current_period_end=NOW + timedelta(days=10), conversations_used=0, conversation_limit=1500,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- subscriptions.py ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "gstin,expected",
    [
        (None, True), ("", True), ("garbage", True), ("24AAPFU0939F1ZV", True), (" 24aapfu0939f1zv ", True),
        ("27AAPFU0939F1ZV", False), ("29AAPFU0939F1ZV", False), ("2AAPFU0939F1ZV12", True),
    ],
)
def test_is_intra_state(gstin, expected):
    client = SimpleNamespace(gst_number=gstin)
    assert subs.is_intra_state(client, "24") is expected


def test_is_intra_state_honours_a_different_seller_state():
    assert subs.is_intra_state(SimpleNamespace(gst_number="27AAPFU0939F1ZV"), "27") is True


@pytest.mark.parametrize(
    "delta,expected",
    [(timedelta(days=10), 10), (timedelta(days=10, seconds=1), 11), (timedelta(hours=1), 1),
     (timedelta(seconds=0), 0), (timedelta(days=-3), 0)],
)
def test_days_left_rounds_up_and_never_goes_negative(delta, expected):
    assert subs.days_left(_sub(current_period_end=NOW + delta), NOW) == expected


@pytest.mark.parametrize(
    "used,limit,expected", [(0, 1500, 0.0), (1200, 1500, 80.0), (1500, 1500, 100.0), (1800, 1500, 120.0), (5, 0, 0.0), (1, 3, 33.3)]
)
def test_percent_used_is_soft_and_may_exceed_100(used, limit, expected):
    assert subs.percent_used(_sub(conversations_used=used, conversation_limit=limit)) == expected


def test_plan_features_returns_a_plain_copy():
    plan = SimpleNamespace(features={"whatsapp": True})
    out = subs.plan_features(plan)
    assert out == {"whatsapp": True} and out is not plan.features
    assert subs.plan_features(SimpleNamespace(features=None)) == {}


def test_now_utc_is_timezone_aware_utc():
    assert subs.now_utc().tzinfo is timezone.utc


def test_checkout_error_carries_code_message_and_status():
    err = subs.CheckoutError("x", "msg", 429)
    assert (err.code, err.message, err.http_status, str(err)) == ("x", "msg", 429, "msg")
    assert subs.CheckoutError("y", "m").http_status == 400


def test_legacy_plan_slug_mapping_covers_the_three_seeded_plans():
    assert subs.LEGACY_PLAN_SLUG == {"starter_1500": "starter", "growth_5000": "growth", "pro_9000": "pro"}


# --- checkout.py --------------------------------------------------------------------------

def test_description_names_the_plan_and_the_purpose():
    plan = SimpleNamespace(name="Growth", billing_period_days=30)
    mk = lambda purpose: SimpleNamespace(plan=plan, purpose=purpose)  # noqa: E731
    assert checkout_mod._description(mk("new")) == "SellerTalk24 Growth — 30 days"
    assert checkout_mod._description(mk("upgrade")) == "SellerTalk24 Growth — 30 days (upgrade)"
    assert checkout_mod._description(mk("renewal")) == "SellerTalk24 Growth — 30 days (renewal)"


def test_receipt_is_unique_prefixed_and_within_razorpays_40_chars():
    receipts = {checkout_mod._receipt(12345) for _ in range(200)}
    assert len(receipts) == 200
    assert all(r.startswith("st24_12345_") and len(r) <= 40 for r in receipts)
    assert len(checkout_mod._receipt(10**12)) <= 40


def test_checkout_rate_limit_constant_is_ten_per_minute():
    assert checkout_mod.CHECKOUTS_PER_MINUTE == 10


# --- webhook.py ---------------------------------------------------------------------------

def test_entity_digs_out_razorpays_nested_entity_or_empty():
    payload = {"payload": {"payment": {"entity": {"id": "pay_1"}}}}
    assert webhook_mod._entity(payload, "payment") == {"id": "pay_1"}
    assert webhook_mod._entity(payload, "order") == {}
    assert webhook_mod._entity({}, "payment") == {}
    assert webhook_mod._entity({"payload": None}, "payment") == {}


def test_fallback_event_id_is_a_stable_body_hash():
    body = b'{"event":"x"}'
    assert webhook_mod.fallback_event_id(body) == "body-sha256:" + hashlib.sha256(body).hexdigest()
    assert webhook_mod.fallback_event_id(body) != webhook_mod.fallback_event_id(body + b" ")


# --- views.py -----------------------------------------------------------------------------

def test_amounts_out_maps_every_field_including_the_gst_total():
    b = AmountBreakdown(base_paise=100, credit_paise=10, taxable_paise=90, cgst_paise=8, sgst_paise=8, igst_paise=0, total_paise=106)
    out = views.amounts_out(b)
    assert out.model_dump() == {
        "base_paise": 100, "credit_paise": 10, "taxable_paise": 90, "cgst_paise": 8,
        "sgst_paise": 8, "igst_paise": 0, "gst_paise": 16, "total_paise": 106,
    }


def test_max_page_size_is_capped():
    assert views.MAX_PAGE_SIZE == 100


# --- activation.py ------------------------------------------------------------------------

def test_activation_error_carries_code_status_and_retry_hint():
    err = ActivationError("c", "m", http_status=409, retryable=True)
    assert (err.code, err.message, err.http_status, err.retryable) == ("c", "m", 409, True)
    assert ActivationError("c", "m").retryable is False


def test_check_payment_accepts_only_a_captured_payment_for_this_order():
    _check_payment({"order_id": "o1", "status": "captured"}, "o1")   # no exception


@pytest.mark.parametrize(
    "payment,code,retryable",
    [
        ({"order_id": "other", "status": "captured"}, "payment_order_mismatch", False),
        ({"order_id": "o1", "status": "authorized"}, "payment_not_captured", True),
        ({"order_id": "o1", "status": "created"}, "payment_not_captured", True),
        ({"order_id": "o1", "status": "failed"}, "payment_failed", False),
        ({"order_id": "o1", "status": "refunded"}, "payment_refunded", False),
        ({"status": "captured"}, "payment_order_mismatch", False),
    ],
)
def test_check_payment_rejects_everything_else(payment, code, retryable):
    with pytest.raises(ActivationError) as err:
        _check_payment(payment, "o1")
    assert err.value.code == code and err.value.retryable is retryable


# --- router helpers -----------------------------------------------------------------------

def test_http_maps_billing_errors_to_a_structured_detail():
    exc = _http(ActivationError("amount_mismatch", "nope", http_status=400))
    assert exc.status_code == 400 and exc.detail == {"code": "amount_mismatch", "message": "nope"}
    exc = _http(subs.CheckoutError("rate_limited", "slow down", 429))
    assert exc.status_code == 429 and exc.detail["code"] == "rate_limited"


def _settings(**kw):
    """Settings-shaped stand-in for the gateway factory."""
    base = dict(razorpay_key_id="", razorpay_key_secret="", razorpay_webhook_secret="", razorpay_mode="test")
    base.update(kw)
    explicit = base.pop("billing_mock", None)
    base["billing_mock_enabled"] = explicit if explicit is not None else not (base["razorpay_key_id"] and base["razorpay_key_secret"])
    return SimpleNamespace(**base)


def test_get_gateway_returns_mock_or_real_per_settings():
    assert isinstance(get_gateway(_settings()), MockRazorpayClient)
    real = get_gateway(_settings(razorpay_key_id="rzp_test_a", razorpay_key_secret="s"))
    assert isinstance(real, RazorpayClient)


def test_get_gateway_answers_503_when_billing_is_misconfigured():
    with pytest.raises(HTTPException) as err:
        get_gateway(_settings(razorpay_mode="live"))     # mock in live mode is refused
    assert err.value.status_code == 503
    assert "mode" not in str(err.value.detail).lower()   # internal reason is not leaked to the client


def test_payment_order_model_is_what_the_views_query():
    assert PaymentOrder.__tablename__ == "payment_orders"
