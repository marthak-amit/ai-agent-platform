"""Unit tests (no database) for refund maths, gateway mode helpers and the quote's grace look-back."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.billing import credit_notes
from app.services.billing.razorpay_client import (
    MockRazorpayClient,
    RazorpayClient,
    mode_for_key,
    mode_for_settings,
)

ORDER = SimpleNamespace(
    amount_paise=542682, taxable_paise=459900, cgst_paise=41391, sgst_paise=41391, igst_paise=0,
)
ORDER_IGST = SimpleNamespace(amount_paise=542682, taxable_paise=459900, cgst_paise=0, sgst_paise=0, igst_paise=82782)


def test_a_full_refund_returns_the_orders_own_figures_exactly():
    assert credit_notes.split_refund(ORDER, 542682) == (459900, 41391, 41391, 0)
    assert credit_notes.split_refund(ORDER_IGST, 542682) == (459900, 0, 0, 82782)


def test_a_partial_refund_splits_gst_proportionally_and_always_sums_to_the_amount():
    for amount in (1, 99, 100_000, 271_341, 542_681):
        taxable, cgst, sgst, igst = credit_notes.split_refund(ORDER, amount)
        assert taxable + cgst + sgst + igst == amount
        assert cgst == sgst and igst == 0 and taxable >= 0
    taxable, cgst, sgst, igst = credit_notes.split_refund(ORDER_IGST, 271341)
    assert (cgst, sgst) == (0, 0) and igst == round(271341 * 82782 / 542682) and taxable + igst == 271341


@pytest.mark.parametrize("amount", [0, -5])
def test_a_non_positive_refund_splits_to_nothing(amount):
    assert credit_notes.split_refund(ORDER, amount) == (0, 0, 0, 0)


def test_mode_for_key_only_treats_rzp_live_keys_as_live():
    assert mode_for_key("rzp_live_abc") == "live"
    assert mode_for_key("rzp_test_abc") == "test"
    assert mode_for_key("") == "test" and mode_for_key("garbage") == "test"


def test_gateways_report_their_mode():
    assert MockRazorpayClient().mode == "test"
    assert RazorpayClient("rzp_test_x", "s", "w").mode == "test"
    assert RazorpayClient("rzp_live_x", "s", "w").mode == "live"


@pytest.mark.parametrize(
    "mock, mode, expected",
    [(True, "live", "test"), (True, "test", "test"), (False, "test", "test"), (False, "live", "live")],
)
def test_mode_for_settings(mock, mode, expected):
    settings = SimpleNamespace(billing_mock_enabled=mock, razorpay_mode=mode)
    assert mode_for_settings(settings) == expected
