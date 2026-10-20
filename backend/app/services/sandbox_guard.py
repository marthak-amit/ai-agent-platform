"""
Identity of the in-dashboard simulator's fake customer, and the checks that keep it off every real channel.

The simulator (app/services/simulator_service.py) plays a customer named `sandbox_<client_id>` against the real
router + order engine. That customer must never receive a WhatsApp/Instagram message, never cost the owner an
alert, never reserve or deduct real stock and never show up in orders, customers, analytics or usage. This module
is deliberately dependency-free so the lowest layers (outbound.py, order_service.py) can import it.
"""

from __future__ import annotations

#: Every simulator customer key starts with this; no real WhatsApp number or IGSID can.
SANDBOX_PHONE_PREFIX = "sandbox_"


def sandbox_phone(client_id: int) -> str:
    """Return the simulator customer's phone key for *client_id* (one per tenant)."""
    return f"{SANDBOX_PHONE_PREFIX}{client_id}"


def is_sandbox_phone(value: object) -> bool:
    """True when *value* is a simulator customer key (never a real recipient)."""
    return isinstance(value, str) and value.startswith(SANDBOX_PHONE_PREFIX)


def not_sandbox_phone(column):
    """SQL filter: *column* (a phone / customer_key column) is NOT a simulator customer. Use in owner-facing lists."""
    return ~column.startswith(SANDBOX_PHONE_PREFIX, autoescape=True)
