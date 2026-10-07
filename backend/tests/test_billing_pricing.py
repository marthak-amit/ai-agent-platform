"""Unit tests for app/services/billing/pricing.py — GST modes, rounding, minimum charge, upgrade credit."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.billing import pricing
from app.services.billing.pricing import (
    MIN_CHARGE_PAISE,
    AmountBreakdown,
    compute_amounts,
    compute_upgrade_credit,
    round_half_up_div,
)

STARTER = SimpleNamespace(price_paise=459900)
GROWTH = SimpleNamespace(price_paise=1199900)
PRO = SimpleNamespace(price_paise=1799900)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _amounts(plan, credit=0, *, inclusive=False, intra=True, rate=1800) -> AmountBreakdown:
    """compute_amounts with explicit settings (no env needed)."""
    return compute_amounts(
        plan, credit, prices_include_gst=inclusive, gst_rate_bps=rate, intra_state=intra
    )


# --- round_half_up_div ----------------------------------------------------------

@pytest.mark.parametrize(
    "n,d,expected",
    [(0, 5, 0), (1, 2, 1), (3, 2, 2), (5, 2, 3), (4, 3, 1), (5, 3, 2), (225, 10, 23), (224, 10, 22)],
)
def test_round_half_up_div(n, d, expected):
    assert round_half_up_div(n, d) == expected


def test_round_half_up_div_rejects_bad_input():
    with pytest.raises(ValueError):
        round_half_up_div(-1, 2)
    with pytest.raises(ValueError):
        round_half_up_div(1, 0)


# --- compute_amounts: exclusive (default business mode) ---------------------------

def test_exclusive_intra_state_adds_cgst_sgst_on_top():
    a = _amounts(STARTER)
    assert (a.base_paise, a.credit_paise, a.taxable_paise) == (459900, 0, 459900)
    assert (a.cgst_paise, a.sgst_paise, a.igst_paise) == (41391, 41391, 0)
    assert a.gst_paise == 82782
    assert a.total_paise == 542682  # ₹4,599 + 18% = ₹5,426.82


@pytest.mark.parametrize(
    "plan,total", [(STARTER, 542682), (GROWTH, 1415882), (PRO, 2123882)]
)
def test_exclusive_totals_for_all_three_plans(plan, total):
    assert _amounts(plan).total_paise == total


def test_exclusive_inter_state_uses_igst_only():
    a = _amounts(STARTER, intra=False)
    assert (a.cgst_paise, a.sgst_paise, a.igst_paise) == (0, 0, 82782)
    assert a.total_paise == 542682


def test_exclusive_credit_is_deducted_before_gst():
    a = _amounts(GROWTH, 100000)
    assert (a.credit_paise, a.taxable_paise) == (100000, 1099900)
    assert a.cgst_paise == a.sgst_paise == 98991
    assert a.total_paise == 1099900 + 2 * 98991 == 1297882


def test_exclusive_rounds_half_up_not_bankers():
    # 250 * 9% = 22.5 -> half-up 23 (banker's rounding would give 22).
    a = _amounts(SimpleNamespace(price_paise=250))
    assert (a.cgst_paise, a.sgst_paise) == (23, 23)
    assert a.total_paise == 296


def test_exclusive_igst_and_cgst_sgst_can_differ_by_a_paisa_each_rounded_independently():
    intra = _amounts(SimpleNamespace(price_paise=250), intra=True)
    inter = _amounts(SimpleNamespace(price_paise=250), intra=False)
    assert inter.igst_paise == 45 and intra.gst_paise == 46


def test_exclusive_zero_rate_means_no_gst():
    a = _amounts(STARTER, rate=0)
    assert a.gst_paise == 0 and a.total_paise == 459900


# --- compute_amounts: inclusive ---------------------------------------------------

def test_inclusive_total_equals_price_and_gst_is_backed_out():
    a = _amounts(STARTER, inclusive=True)
    assert a.total_paise == 459900
    assert a.taxable_paise == 389746          # 459900 / 1.18 = 389745.76
    assert a.gst_paise == 70154
    assert (a.cgst_paise, a.sgst_paise, a.igst_paise) == (35077, 35077, 0)


def test_inclusive_inter_state_is_one_igst_line():
    a = _amounts(STARTER, inclusive=True, intra=False)
    assert (a.cgst_paise, a.sgst_paise, a.igst_paise) == (0, 0, 70154)
    assert a.total_paise == 459900


def test_inclusive_odd_gst_paisa_goes_to_cgst_and_totals_still_tie():
    a = _amounts(SimpleNamespace(price_paise=100), inclusive=True)
    assert a.total_paise == 100 and a.taxable_paise == 85 and a.gst_paise == 15
    assert (a.cgst_paise, a.sgst_paise) == (8, 7)


def test_inclusive_credit_reduces_the_gross_charge():
    a = _amounts(STARTER, 100000, inclusive=True)
    assert a.total_paise == 359900 and a.credit_paise == 100000
    assert a.taxable_paise == 305000 and a.gst_paise == 54900


# --- minimum charge ---------------------------------------------------------------

def test_exclusive_total_never_below_minimum_and_applied_credit_shrinks():
    a = _amounts(STARTER, credit=459900)  # credit equal to the whole price
    assert a.total_paise == MIN_CHARGE_PAISE
    assert a.taxable_paise == 84 and a.gst_paise == 16
    assert a.credit_paise == 459900 - 84   # only the credit that could be used
    assert a.taxable_paise + a.credit_paise == a.base_paise


def test_exclusive_credit_larger_than_price_is_capped():
    a = _amounts(STARTER, credit=10_000_000)
    assert a.total_paise == MIN_CHARGE_PAISE and a.credit_paise < 459900


def test_inclusive_total_never_below_minimum():
    a = _amounts(STARTER, credit=10_000_000, inclusive=True)
    assert a.total_paise == MIN_CHARGE_PAISE
    assert a.credit_paise == 459900 - MIN_CHARGE_PAISE


@pytest.mark.parametrize("inclusive", [False, True])
def test_plan_priced_below_minimum_is_rejected(inclusive):
    with pytest.raises(ValueError, match="minimum"):
        _amounts(SimpleNamespace(price_paise=50), inclusive=inclusive)


def test_negative_credit_and_bad_rate_are_rejected():
    with pytest.raises(ValueError):
        _amounts(STARTER, -1)
    with pytest.raises(ValueError):
        _amounts(STARTER, rate=-1)
    with pytest.raises(ValueError):
        _amounts(STARTER, rate=10001)


@pytest.mark.parametrize("inclusive", [False, True])
@pytest.mark.parametrize("intra", [True, False])
def test_invariants_hold_across_prices_and_credits(inclusive, intra):
    for price in (100, 101, 250, 999, 459900, 1199900, 1799900):
        for credit in (0, 1, 49, 100, 12345, price - 1, price, price * 2):
            a = _amounts(SimpleNamespace(price_paise=price), credit, inclusive=inclusive, intra=intra)
            assert a.total_paise >= MIN_CHARGE_PAISE
            assert a.taxable_paise + a.gst_paise == a.total_paise
            assert 0 <= a.credit_paise <= price
            assert (a.cgst_paise + a.sgst_paise == 0) != (a.igst_paise == 0) or a.gst_paise == 0
            assert all(isinstance(v, int) for v in (a.total_paise, a.gst_paise, a.taxable_paise))


def test_breakdown_is_immutable():
    a = _amounts(STARTER)
    with pytest.raises(Exception):
        a.total_paise = 1


# --- defaults come from settings ----------------------------------------------------

def test_defaults_are_read_from_settings(monkeypatch):
    monkeypatch.setattr(
        pricing, "get_settings", lambda: SimpleNamespace(prices_include_gst=True, gst_rate_bps=1200)
    )
    a = compute_amounts(STARTER)
    assert a.total_paise == 459900  # inclusive
    assert a.gst_paise == 459900 - round_half_up_div(459900 * 10000, 11200)


def test_exclusive_is_the_setting_default(monkeypatch):
    monkeypatch.setattr(
        pricing, "get_settings", lambda: SimpleNamespace(prices_include_gst=False, gst_rate_bps=1800)
    )
    assert compute_amounts(STARTER).total_paise == 542682


def test_settings_not_touched_when_both_overrides_given(monkeypatch):
    def boom():
        raise AssertionError("settings must not be read")

    monkeypatch.setattr(pricing, "get_settings", boom)
    assert compute_amounts(STARTER, 0, prices_include_gst=False, gst_rate_bps=1800).total_paise == 542682


# --- compute_upgrade_credit ---------------------------------------------------------

def _sub(status="active", start=T0, days=30) -> SimpleNamespace:
    """A subscription-shaped object covering *days* from *start*."""
    return SimpleNamespace(
        status=status, current_period_start=start, current_period_end=start + timedelta(days=days)
    )


def test_credit_on_day_zero_is_the_full_amount():
    assert compute_upgrade_credit(_sub(), T0, paid_paise=459900) == 459900


def test_credit_halfway_is_half():
    assert compute_upgrade_credit(_sub(), T0 + timedelta(days=15), paid_paise=459900) == 229950


def test_credit_on_the_last_day_is_one_thirtieth():
    now = T0 + timedelta(days=29)
    assert compute_upgrade_credit(_sub(), now, paid_paise=459900) == 15330


def test_credit_one_second_before_end_is_tiny_but_not_negative():
    now = T0 + timedelta(days=30) - timedelta(seconds=1)
    credit = compute_upgrade_credit(_sub(), now, paid_paise=459900)
    assert credit == 0  # 459900 * 1s / 2,592,000s = 0.18 paise -> 0


def test_credit_at_or_after_period_end_is_zero():
    end = T0 + timedelta(days=30)
    assert compute_upgrade_credit(_sub(), end, paid_paise=459900) == 0
    assert compute_upgrade_credit(_sub(), end + timedelta(days=3), paid_paise=459900) == 0


def test_credit_before_period_start_is_clamped_to_the_full_amount():
    assert compute_upgrade_credit(_sub(), T0 - timedelta(days=2), paid_paise=459900) == 459900


@pytest.mark.parametrize("status", ["expired", "cancelled", "pending", "superseded"])
def test_credit_is_zero_unless_the_subscription_is_active(status):
    assert compute_upgrade_credit(_sub(status=status), T0 + timedelta(days=1), paid_paise=459900) == 0


def test_credit_is_zero_when_nothing_was_paid():
    assert compute_upgrade_credit(_sub(), T0 + timedelta(days=1), paid_paise=0) == 0


def test_credit_rounds_half_up_and_never_exceeds_paid():
    sub = _sub(days=2)
    assert compute_upgrade_credit(sub, T0 + timedelta(days=1), paid_paise=3) == 2   # 1.5 -> 2
    assert compute_upgrade_credit(sub, T0, paid_paise=3) == 3
    for hours in range(0, 49):
        assert 0 <= compute_upgrade_credit(sub, T0 + timedelta(hours=hours), paid_paise=3) <= 3


def test_credit_decreases_monotonically_through_the_period():
    values = [
        compute_upgrade_credit(_sub(), T0 + timedelta(days=d), paid_paise=459900) for d in range(0, 31)
    ]
    assert values == sorted(values, reverse=True)
    assert values[0] == 459900 and values[-1] == 0


def test_credit_rejects_naive_datetimes_and_negative_paid():
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_upgrade_credit(_sub(), datetime(2026, 10, 2), paid_paise=100)
    with pytest.raises(ValueError):
        compute_upgrade_credit(_sub(), T0, paid_paise=-1)


def test_upgrade_flow_credit_then_amounts_end_to_end():
    """Starter bought on day 0 (₹4,599 taxable), upgrade to Growth on day 15."""
    credit = compute_upgrade_credit(_sub(), T0 + timedelta(days=15), paid_paise=459900)
    a = _amounts(GROWTH, credit)
    assert credit == 229950
    assert a.taxable_paise == 1199900 - 229950 == 969950
    assert a.cgst_paise == a.sgst_paise == 87296 + 0  # 969950 * 9% = 87295.5 -> 87296
    assert a.total_paise == 969950 + 2 * 87296
