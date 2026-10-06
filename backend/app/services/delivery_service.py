"""Utility for computing a human-readable delivery time string."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def get_delivery_time_str(product=None, client=None) -> str:
    """
    Return a human-readable delivery time string for a product/client combination.

    Priority:
    1. product.delivery_days if set → "{n} business day(s)"
    2. client.delivery_days_min + delivery_days_max → "{min}–{max} business days"
    3. Safe fallback → "3–7 business days"

    Args:
        product: Product ORM instance (optional).
        client:  Client ORM instance (optional).

    Returns:
        Delivery time string, e.g. "1 business day" or "3–7 business days".
    """
    if product is not None:
        days = getattr(product, "delivery_days", None)
        if days:
            unit = "business day" if days == 1 else "business days"
            return f"{days} {unit}"

    if client is not None:
        min_days = getattr(client, "delivery_days_min", None)
        max_days = getattr(client, "delivery_days_max", None)
        if min_days and max_days:
            return f"{min_days}–{max_days} business days"

    return "3–7 business days"


# ── Order ETA window (computed by the engine; the LLM never states dates) ────

_IST = ZoneInfo("Asia/Kolkata")
# The order's delivery clock only runs once it is confirmed: a UPI order waits for payment
# approval, a COD order starts at creation.
_PRE_CONFIRMATION_STATUSES = frozenset({"new", "pending_payment", "payment_submitted"})
_NO_ETA_STATUSES = frozenset({"cancelled", "delivered"})


def to_ist_date(dt: datetime | None) -> date | None:
    """Calendar date of a timestamp in IST (naive timestamps are taken as UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).date()


def add_business_days(start: date, days: int) -> date:
    """`start` plus `days` Monday–Friday business days (days <= 0 returns `start`)."""
    current = start
    remaining = max(int(days), 0)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def order_delivery_days(client=None, product_days: list[int | None] | None = None) -> tuple[int, int]:
    """
    (min_days, max_days) of business days from confirmation to delivery.

    Same priority as get_delivery_time_str(): product.delivery_days (the slowest line wins when
    every line has one) → client.delivery_days_min/max → 3–7.
    """
    if product_days and all(d for d in product_days):
        slowest = max(int(d) for d in product_days if d)
        return slowest, slowest
    lo = getattr(client, "delivery_days_min", None)
    hi = getattr(client, "delivery_days_max", None)
    if lo and hi:
        return int(lo), int(max(lo, hi))
    return 3, 7


def order_eta_window(
    order, client=None, product_days: list[int | None] | None = None, today: date | None = None,
) -> dict:
    """
    Engine-computed delivery estimate for one order.

    Returns {"state": ..., "from": date|None, "to": date|None, "days": (min, max)} where state is
      "none"       — cancelled/delivered (no estimate applies),
      "awaiting"   — not confirmed yet (UPI payment pending/under review): the clock hasn't started,
      "window"     — expected between `from` and `to`,
      "overdue"    — the window has already passed and the order isn't delivered.
    The clock starts at paid_at / confirmed_at (falling back to created_at for COD orders).
    """
    status = getattr(order, "status", None) or "new"
    days = order_delivery_days(client, product_days)
    if status in _NO_ETA_STATUSES:
        return {"state": "none", "from": None, "to": None, "days": days}
    is_cod = (getattr(order, "payment_method", None) or "").upper() == "COD"
    if status in _PRE_CONFIRMATION_STATUSES and not is_cod:
        return {"state": "awaiting", "from": None, "to": None, "days": days}
    start = (
        to_ist_date(getattr(order, "paid_at", None))
        or to_ist_date(getattr(order, "confirmed_at", None))
        or to_ist_date(getattr(order, "created_at", None))
    )
    if start is None:
        return {"state": "awaiting", "from": None, "to": None, "days": days}
    window_from, window_to = add_business_days(start, days[0]), add_business_days(start, days[1])
    today = today or datetime.now(_IST).date()
    state = "overdue" if today > window_to else "window"
    return {"state": state, "from": window_from, "to": window_to, "days": days}
