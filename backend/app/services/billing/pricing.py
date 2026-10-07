"""
Plan pricing, GST and upgrade-credit maths for SellerTalk24 billing.

Pure functions only: no database, no clock, no settings reads beyond defaults, so
every rule is unit-testable. All money is integer paise; rounding is half-up
(never banker's rounding, never floats).

GST modes (settings.prices_include_gst):
  * False (default) — plan prices are EXCLUSIVE: GST is added on top of the net.
  * True            — plan prices are INCLUSIVE: the charge equals the (net) price
                      and GST is back-calculated out of it.

A discount (upgrade credit) is always applied BEFORE GST, in the same basis as the
plan price: taxable value when exclusive, gross when inclusive.

Tax split: intra-state supply -> CGST + SGST (half the rate each); inter-state -> IGST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config import get_settings

MIN_CHARGE_PAISE = 100  # Razorpay will not create an order below ₹1.00
_BPS = 10_000
_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class AmountBreakdown:
    """
    The full price build-up for one checkout, in paise.

    base_paise    plan list price (exclusive or inclusive per the GST mode)
    credit_paise  upgrade credit actually applied (may be less than requested; see
                  compute_amounts for the Razorpay minimum)
    taxable_paise value GST is charged on (net of credit, excluding GST)
    cgst/sgst/igst the GST split; exactly one of (cgst+sgst) / igst is non-zero
    total_paise   what Razorpay is asked to charge = taxable + GST
    """

    base_paise: int
    credit_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    total_paise: int

    @property
    def gst_paise(self) -> int:
        """Total GST = CGST + SGST + IGST."""
        return self.cgst_paise + self.sgst_paise + self.igst_paise


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Return numerator/denominator rounded half-up to an int (both must be >= 0 / > 0)."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("round_half_up_div needs numerator >= 0 and denominator > 0")
    return (2 * numerator + denominator) // (2 * denominator)


def _gst_on_taxable(taxable: int, rate_bps: int, intra_state: bool) -> tuple[int, int, int]:
    """Return (cgst, sgst, igst) charged on an exclusive *taxable* value."""
    if intra_state:
        half = round_half_up_div(taxable * rate_bps, 2 * _BPS)
        return half, half, 0
    return 0, 0, round_half_up_div(taxable * rate_bps, _BPS)


def _split_inclusive_gst(gst: int, intra_state: bool) -> tuple[int, int, int]:
    """Split an already-fixed GST amount (inclusive mode): an odd paisa goes to CGST."""
    if not intra_state:
        return 0, 0, gst
    sgst = gst // 2
    return gst - sgst, sgst, 0


def compute_amounts(
    plan: Any,
    credit_paise: int = 0,
    *,
    prices_include_gst: bool | None = None,
    gst_rate_bps: int | None = None,
    intra_state: bool = True,
) -> AmountBreakdown:
    """
    Build the checkout amounts for *plan* after applying *credit_paise*.

    Args:
        plan:               Anything with a ``price_paise`` int (a BillingPlan row).
        credit_paise:       Upgrade credit to deduct, in the same basis as the plan
                            price (taxable when exclusive, gross when inclusive).
        prices_include_gst: Override settings.prices_include_gst.
        gst_rate_bps:       Override settings.gst_rate_bps (1800 = 18%).
        intra_state:        True -> CGST+SGST, False -> IGST. Callers default a
                            client with no billing state to intra-state (Gujarat).

    Returns:
        AmountBreakdown. ``credit_paise`` on the result is the credit actually
        applied: it is reduced if applying it all would push the total under
        MIN_CHARGE_PAISE (the total is never below ₹1.00).

    Raises:
        ValueError: negative credit, invalid rate, or a plan whose own price
                    cannot reach the Razorpay minimum.
    """
    if prices_include_gst is None or gst_rate_bps is None:
        settings = get_settings()  # only touched when a default is needed
        if prices_include_gst is None:
            prices_include_gst = settings.prices_include_gst
        if gst_rate_bps is None:
            gst_rate_bps = settings.gst_rate_bps
    inclusive, rate = prices_include_gst, gst_rate_bps
    price = int(plan.price_paise)

    if credit_paise < 0:
        raise ValueError("credit_paise must be >= 0")
    if not 0 <= rate <= _BPS:
        raise ValueError("gst_rate_bps must be between 0 and 10000")

    if inclusive:
        return _compute_inclusive(price, credit_paise, rate, intra_state)
    return _compute_exclusive(price, credit_paise, rate, intra_state)


def _compute_exclusive(price: int, credit: int, rate: int, intra_state: bool) -> AmountBreakdown:
    """Prices exclude GST: GST is added on top of (price - credit)."""
    if price + sum(_gst_on_taxable(price, rate, intra_state)) < MIN_CHARGE_PAISE:
        raise ValueError(f"plan price {price} paise is below the {MIN_CHARGE_PAISE}-paise minimum charge")

    taxable = max(price - credit, 0)
    # Never charge under the minimum: shrink the applied credit until the total reaches it.
    while taxable + sum(_gst_on_taxable(taxable, rate, intra_state)) < MIN_CHARGE_PAISE:
        taxable += 1

    cgst, sgst, igst = _gst_on_taxable(taxable, rate, intra_state)
    return AmountBreakdown(
        base_paise=price,
        credit_paise=price - taxable,
        taxable_paise=taxable,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        total_paise=taxable + cgst + sgst + igst,
    )


def _compute_inclusive(price: int, credit: int, rate: int, intra_state: bool) -> AmountBreakdown:
    """Prices include GST: the charge is (price - credit) and GST is backed out of it."""
    if price < MIN_CHARGE_PAISE:
        raise ValueError(f"plan price {price} paise is below the {MIN_CHARGE_PAISE}-paise minimum charge")

    total = max(price - credit, MIN_CHARGE_PAISE)
    taxable = round_half_up_div(total * _BPS, _BPS + rate)
    gst = total - taxable
    cgst, sgst, igst = _split_inclusive_gst(gst, intra_state)
    return AmountBreakdown(
        base_paise=price,
        credit_paise=price - total,
        taxable_paise=taxable,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        total_paise=total,
    )


def compute_upgrade_credit(active_sub: Any, now: datetime, *, paid_paise: int) -> int:
    """
    Pro-rata value, in paise, of the unused part of the active subscription's period.

    Credit = paid_paise × (time remaining / period length), rounded half-up, measured
    to the microsecond. Day 0 of the period gives the full amount; it falls linearly to
    0 at current_period_end. Nothing is credited for a subscription that is not
    ``active`` or whose period has ended. Timestamps are clamped to the period, so a
    clock slightly before the start gives the full amount, never more.

    Args:
        active_sub: Anything with ``status``, ``current_period_start`` and
                    ``current_period_end`` (tz-aware datetimes).
        now:        The current time (tz-aware); injected so callers/tests control it.
        paid_paise: What the customer paid for this period, in the basis the new
                    price uses (taxable value when prices are exclusive). The caller
                    supplies it from the source payment order — normally
                    taxable_paise + credited_paise, i.e. cash paid plus credit carried in.

    Returns:
        Credit in paise: 0 <= credit <= paid_paise.
    """
    if paid_paise < 0:
        raise ValueError("paid_paise must be >= 0")
    start, end = active_sub.current_period_start, active_sub.current_period_end
    if now.tzinfo is None or start.tzinfo is None or end.tzinfo is None:
        raise ValueError("compute_upgrade_credit needs timezone-aware datetimes")
    if active_sub.status != "active" or paid_paise == 0 or now >= end:
        return 0

    effective_now = max(now, start)
    remaining = (end - effective_now) // _MICROSECOND
    total = (end - start) // _MICROSECOND
    if total <= 0:
        return 0
    return min(round_half_up_div(paid_paise * remaining, total), paid_paise)
