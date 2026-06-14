"""
Unit tests for the confirm-style slot fix (PARTS 2–4).

Scenarios:
  A  Slot sequence: color→size→qty→name→address(yes=saved)→payment(yes=UPI)→summary
  B  Resending "yes" after confirm → same result (idempotency at parse layer)
  C  Address "change" → None returned → next prompt is ask_address (fresh)
  D  Payment loop must NOT occur — "yes" fills UPI on first call
  E  mid-flow "Hello" → intent=OTHER → slot unchanged, re-ask returned
  F  _last_agent_offered_upi_only detection
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock

from app.services.conversation_flow import (
    extract_order_field,
    get_next_required_slot,
    _last_agent_offered_upi_only,
    _last_agent_offered_saved_address,
    _SLOT_AFFIRMATIVES,
    _SAVED_ADDRESS_AFFIRMATIONS,
    _SAVED_ADDRESS_NEGATIONS,
)
from app.services.language_templates import get_template


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conv(
    color=None, size=None, material=None, qty=None,
    name=None, address=None, payment=None,
):
    """Build a minimal mock Conversation with slot fields."""
    c = MagicMock()
    c.selected_color = color
    c.selected_size = size
    c.selected_material = material
    c.pending_order_quantity = qty
    c.customer_name = name
    c.delivery_address = address
    c.payment_method = payment
    return c


_NO_VARIANTS = {"has_variants": False, "needs_color": False, "needs_size": False, "needs_material": False}
_WITH_VARIANTS = {
    "has_variants": True,
    "needs_color": True,
    "needs_size": True,
    "needs_material": False,
    "available_colors": ["Red", "Blue", "Black"],
    "available_sizes": ["S", "M", "L", "XL"],
    "available_materials": [],
}

_HISTORY_UPI_ONLY = [
    {"role": "assistant", "content": "Payment: UPI ✅ Confirm? (yes)"},
]
_HISTORY_UPI_COD = [
    {"role": "assistant", "content": "Payment? UPI / COD"},
]
_HISTORY_SAVED_ADDR = [
    {"role": "assistant", "content": "Deliver to 42 Baker St, London? (yes/change)"},
]
_HISTORY_ASK_PAYMENT = [
    {"role": "assistant", "content": "How would you like to pay — UPI?"},
]


# ── F: helper detection ───────────────────────────────────────────────────────

def test_last_agent_offered_upi_only_confirm_style():
    assert _last_agent_offered_upi_only(_HISTORY_UPI_ONLY) is True

def test_last_agent_offered_upi_only_question_style():
    assert _last_agent_offered_upi_only(_HISTORY_ASK_PAYMENT) is True

def test_last_agent_offered_upi_only_when_cod_present():
    assert _last_agent_offered_upi_only(_HISTORY_UPI_COD) is False

def test_last_agent_offered_saved_address_detection():
    assert _last_agent_offered_saved_address(_HISTORY_SAVED_ADDR) is True

def test_affirmatives_set():
    for word in ("yes", "haan", "ha", "ok", "okay", "sahi", "theek"):
        assert word in _SLOT_AFFIRMATIVES, f"{word!r} missing from _SLOT_AFFIRMATIVES"


# ── D: payment loop fix — "yes" fills UPI on first call ──────────────────────

def test_payment_yes_fills_upi_when_upi_only():
    """Scenario D — 'yes' must fill UPI when agent offered UPI-only prompt."""
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    # next_slot should be payment_method
    assert get_next_required_slot(conv, _WITH_VARIANTS) == "payment_method"

    for affirmative in ("yes", "haan", "ha", "ok", "okay", "sahi", "theek"):
        result = extract_order_field(
            conv, affirmative,
            variant_info=_WITH_VARIANTS,
            conversation_history=_HISTORY_UPI_ONLY,
        )
        assert result == ("payment_method", "UPI"), (
            f"'{affirmative}' should fill UPI when UPI-only was offered, got {result!r}"
        )


def test_payment_yes_returns_none_when_cod_also_offered():
    """When both UPI and COD were offered, 'yes' is ambiguous — return None, re-ask."""
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    result = extract_order_field(
        conv, "yes",
        variant_info=_WITH_VARIANTS,
        conversation_history=_HISTORY_UPI_COD,
    )
    assert result is None, "Ambiguous 'yes' when COD also offered must return None"


def test_payment_literal_upi_always_fills():
    """Literal 'UPI' always fills regardless of history."""
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    result = extract_order_field(conv, "UPI", variant_info=_WITH_VARIANTS)
    assert result == ("payment_method", "UPI")


def test_payment_literal_cod_always_fills():
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    result = extract_order_field(
        conv, "COD", variant_info=_WITH_VARIANTS,
        conversation_history=_HISTORY_UPI_COD,
    )
    assert result == ("payment_method", "COD")


def test_payment_gpay_fills_upi():
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    result = extract_order_field(conv, "gpay", variant_info=_WITH_VARIANTS)
    assert result == ("payment_method", "UPI")


# ── B: idempotency at parse layer ─────────────────────────────────────────────

def test_payment_yes_idempotent():
    """Resending 'yes' for payment should return the same value both times."""
    conv = _conv(color="Red", size="M", qty=2, name="Amit", address="42 Main St")
    r1 = extract_order_field(conv, "yes", variant_info=_WITH_VARIANTS, conversation_history=_HISTORY_UPI_ONLY)
    r2 = extract_order_field(conv, "yes", variant_info=_WITH_VARIANTS, conversation_history=_HISTORY_UPI_ONLY)
    assert r1 == r2 == ("payment_method", "UPI")


# ── C: address "change" returns None ─────────────────────────────────────────

def test_address_change_returns_none():
    """'change' in response to saved-address confirm must return None (not fill)."""
    conv = _conv(qty=2, name="Amit")
    for word in ("change", "no", "nahi", "nope", "edit"):
        result = extract_order_field(
            conv, word,
            variant_info=_NO_VARIANTS,
            conversation_history=_HISTORY_SAVED_ADDR,
            saved_address="42 Baker St, London",
        )
        assert result is None, f"'{word}' should NOT fill address, got {result!r}"


def test_address_yes_fills_saved():
    """'yes' in response to saved-address confirm must fill the saved address."""
    conv = _conv(qty=2, name="Amit")
    for word in ("yes", "haan", "ok", "sahi"):
        result = extract_order_field(
            conv, word,
            variant_info=_NO_VARIANTS,
            conversation_history=_HISTORY_SAVED_ADDR,
            saved_address="42 Baker St, London",
        )
        assert result == ("delivery_address", "42 Baker St, London"), (
            f"'{word}' should fill saved address, got {result!r}"
        )


# ── A: full slot sequence (no-variant product) ────────────────────────────────

def test_slot_sequence_no_variants():
    """Scenario A — step through all slots for a non-variant product."""
    conv = _conv()

    # Step 1: quantity
    assert get_next_required_slot(conv, _NO_VARIANTS) == "quantity"
    r = extract_order_field(conv, "3", variant_info=_NO_VARIANTS)
    assert r == ("pending_order_quantity", 3)
    conv.pending_order_quantity = 3

    # Step 2: customer_name
    assert get_next_required_slot(conv, _NO_VARIANTS) == "customer_name"
    r = extract_order_field(conv, "Amit Shah", variant_info=_NO_VARIANTS)
    assert r == ("customer_name", "Amit Shah")
    conv.customer_name = "Amit Shah"

    # Step 3: delivery_address
    assert get_next_required_slot(conv, _NO_VARIANTS) == "delivery_address"
    r = extract_order_field(conv, "702 Somerset St, Mumbai", variant_info=_NO_VARIANTS)
    assert r == ("delivery_address", "702 Somerset St, Mumbai")
    conv.delivery_address = "702 Somerset St, Mumbai"

    # Step 4: payment_method — yes fills UPI
    assert get_next_required_slot(conv, _NO_VARIANTS) == "payment_method"
    r = extract_order_field(conv, "yes", variant_info=_NO_VARIANTS, conversation_history=_HISTORY_UPI_ONLY)
    assert r == ("payment_method", "UPI")
    conv.payment_method = "UPI"

    # All slots filled
    assert get_next_required_slot(conv, _NO_VARIANTS) is None


def test_slot_sequence_with_variants():
    """Scenario A — step through all slots for a variant product (color+size)."""
    conv = _conv()
    vi = _WITH_VARIANTS

    # Step 1: color
    assert get_next_required_slot(conv, vi) == "color"
    r = extract_order_field(conv, "Red", variant_info=vi)
    assert r == ("selected_color", "Red")
    conv.selected_color = "Red"

    # Step 2: size
    assert get_next_required_slot(conv, vi) == "size"
    r = extract_order_field(conv, "M", variant_info=vi)
    assert r == ("selected_size", "M")
    conv.selected_size = "M"

    # Step 3: quantity
    assert get_next_required_slot(conv, vi) == "quantity"
    r = extract_order_field(conv, "1", variant_info=vi)
    assert r == ("pending_order_quantity", 1)
    conv.pending_order_quantity = 1

    # Step 4: name
    assert get_next_required_slot(conv, vi) == "customer_name"
    r = extract_order_field(conv, "Priya Mehta", variant_info=vi)
    assert r == ("customer_name", "Priya Mehta")
    conv.customer_name = "Priya Mehta"

    # Step 5: address — via saved addr confirm
    assert get_next_required_slot(conv, vi) == "delivery_address"
    r = extract_order_field(
        conv, "yes", variant_info=vi,
        conversation_history=_HISTORY_SAVED_ADDR,
        saved_address="42 Baker St, London",
    )
    assert r == ("delivery_address", "42 Baker St, London")
    conv.delivery_address = "42 Baker St, London"

    # Step 6: payment — yes fills UPI
    assert get_next_required_slot(conv, vi) == "payment_method"
    r = extract_order_field(conv, "yes", variant_info=vi, conversation_history=_HISTORY_UPI_ONLY)
    assert r == ("payment_method", "UPI")
    conv.payment_method = "UPI"

    # All done
    assert get_next_required_slot(conv, vi) is None


# ── Templates ─────────────────────────────────────────────────────────────────

def test_confirm_payment_upi_template_english():
    t = get_template("english", "confirm_payment_upi")
    assert "UPI" in t
    assert "yes" in t.lower() or "confirm" in t.lower()

def test_confirm_payment_upi_template_hindi():
    t = get_template("hinglish", "confirm_payment_upi")
    assert "UPI" in t

def test_confirm_payment_upi_template_gujarati():
    t = get_template("gujarati_roman", "confirm_payment_upi")
    assert "UPI" in t

def test_ask_payment_upi_cod_template():
    t = get_template("english", "ask_payment_upi_cod")
    assert "UPI" in t and "COD" in t
