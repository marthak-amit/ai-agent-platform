"""Tests for the SellerTalk24 billing settings (defaults, parsing, mock-mode derivation)."""

import pytest
from pydantic import ValidationError

from app.config import Settings

_REQUIRED = dict(
    database_url="postgresql+asyncpg://u:p@localhost/db",
    meta_app_secret="x", meta_verify_token="x",
    whatsapp_access_token="x", whatsapp_phone_number_id="x",
)


def _settings(monkeypatch, env: dict | None = None, **kw) -> Settings:
    """Settings built from explicit kwargs + given env vars, ignoring any real .env file."""
    for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_MODE",
                 "SELLERTALK24_BILLING_MOCK", "PRICES_INCLUDE_GST", "GST_RATE_BPS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None, **_REQUIRED, **kw)


def test_defaults_are_exclusive_18_percent_test_mode_auto_mock(monkeypatch):
    s = _settings(monkeypatch)
    assert s.prices_include_gst is False          # plan prices are GST-exclusive
    assert s.gst_rate_bps == 1800
    assert s.razorpay_mode == "test"
    assert s.sellertalk24_billing_mock is None
    assert s.razorpay_webhook_secret == ""


def test_billing_settings_are_read_from_env_vars(monkeypatch):
    s = _settings(monkeypatch, {
        "RAZORPAY_KEY_ID": "rzp_test_1", "RAZORPAY_KEY_SECRET": "sec", "RAZORPAY_WEBHOOK_SECRET": "whsec",
        "RAZORPAY_MODE": "live", "PRICES_INCLUDE_GST": "true", "GST_RATE_BPS": "1200",
    })
    assert (s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret) == ("rzp_test_1", "sec", "whsec")
    assert s.razorpay_mode == "live" and s.prices_include_gst is True and s.gst_rate_bps == 1200


def test_mock_is_auto_on_when_keys_are_empty(monkeypatch):
    assert _settings(monkeypatch).billing_mock_enabled is True
    assert _settings(monkeypatch, razorpay_key_id="rzp_test_1").billing_mock_enabled is True       # secret missing
    assert _settings(monkeypatch, razorpay_key_secret="sec").billing_mock_enabled is True          # id missing


def test_mock_is_auto_off_when_both_keys_are_set(monkeypatch):
    s = _settings(monkeypatch, razorpay_key_id="rzp_test_1", razorpay_key_secret="sec")
    assert s.billing_mock_enabled is False


def test_explicit_flag_overrides_the_key_based_default(monkeypatch):
    keys = dict(razorpay_key_id="rzp_test_1", razorpay_key_secret="sec")
    assert _settings(monkeypatch, {"SELLERTALK24_BILLING_MOCK": "true"}, **keys).billing_mock_enabled is True
    assert _settings(monkeypatch, {"SELLERTALK24_BILLING_MOCK": "false"}).billing_mock_enabled is False


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_mock_flag_means_auto_not_a_parse_error(monkeypatch, blank):
    s = _settings(monkeypatch, {"SELLERTALK24_BILLING_MOCK": blank})
    assert s.sellertalk24_billing_mock is None and s.billing_mock_enabled is True


@pytest.mark.parametrize("raw,expected", [("LIVE", "live"), (" Test ", "test"), ("live", "live")])
def test_razorpay_mode_is_normalised(monkeypatch, raw, expected):
    assert _settings(monkeypatch, {"RAZORPAY_MODE": raw}).razorpay_mode == expected


def test_invalid_razorpay_mode_is_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, {"RAZORPAY_MODE": "sandbox"})


@pytest.mark.parametrize("bad", ["-1", "10001"])
def test_gst_rate_must_be_between_0_and_10000_bps(monkeypatch, bad):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, {"GST_RATE_BPS": bad})


def test_blank_normaliser_helpers_pass_non_strings_through(monkeypatch):
    assert Settings.blank_billing_mock_means_auto(True) is True
    assert Settings.normalise_razorpay_mode(None) is None
