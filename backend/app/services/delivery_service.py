"""Utility for computing a human-readable delivery time string."""

from __future__ import annotations


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
