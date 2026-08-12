"""
Regression tests for the numeric-size / quantity collision in the variant flow.

Live failure this reproduces (conv=60, Traditional choli PR17761):
    ASSISTANT: Size? 38 / 3XL / 40 / L / M / S / XL / XXL
    USER: 40
    → selected_size='40' AND pending_order_quantity=40

The single token "40" was consumed as the size and then re-scanned by the
multi-slot capture as a quantity, so the quantity question was never asked and
the customer was quoted 40 pieces (₹159,960) for a 1-piece intent.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.conversation_flow import extract_order_field, get_next_required_slot

VARIANT_INFO = {
    "has_variants": True,
    "needs_color": True,
    "needs_size": True,
    "needs_material": False,
    "available_colors": ["Gold", "Green", "Pink", "Red", "White"],
    "available_sizes": ["38", "3XL", "40", "L", "M", "S", "XL", "XXL"],
    "available_materials": [],
}


def _conv(**overrides):
    """Conversation double with all order slots empty unless overridden."""
    base = dict(
        id=60,
        selected_color=None,
        selected_size=None,
        selected_material=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        mobile_number=None,
        payment_method=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_numeric_size_answer_does_not_fill_quantity():
    """"40" answering the size question sets size only — never quantity."""
    conv = _conv(selected_color="Red")

    result = extract_order_field(conv, "40", variant_info=VARIANT_INFO)

    assert result == ("selected_size", "40")
    assert conv.pending_order_quantity is None, (
        "numeric size token was double-counted as quantity"
    )


def test_quantity_is_still_asked_after_numeric_size():
    """With size filled from a numeric token, quantity remains an open slot."""
    conv = _conv(selected_color="Red")
    extract_order_field(conv, "40", variant_info=VARIANT_INFO)
    conv.selected_size = "40"

    assert get_next_required_slot(conv, VARIANT_INFO) == "quantity"


def test_explicit_quantity_alongside_numeric_size_still_captured():
    """"40 size, 2 pieces" → size=40 AND quantity=2 (multi-slot preserved)."""
    conv = _conv(selected_color="Red")

    result = extract_order_field(conv, "40 size, 2 pieces", variant_info=VARIANT_INFO)

    assert result == ("selected_size", "40")
    assert conv.pending_order_quantity == 2


def test_multislot_colour_and_numeric_size_does_not_fill_quantity():
    """"red 40" at the colour slot fills colour+size, leaving quantity unset."""
    conv = _conv()

    result = extract_order_field(conv, "red 40", variant_info=VARIANT_INFO)

    assert result == ("selected_color", "Red")
    assert conv.selected_size == "40"
    assert conv.pending_order_quantity is None


def test_multislot_colour_size_and_quantity_all_captured():
    """The original multi-slot feature still works: "red XL 2 pieces"."""
    conv = _conv()

    result = extract_order_field(conv, "red XL 2 pieces", variant_info=VARIANT_INFO)

    assert result == ("selected_color", "Red")
    assert conv.selected_size == "XL"
    assert conv.pending_order_quantity == 2


def test_quantity_slot_itself_still_accepts_a_number():
    """Answering the quantity question with "10" fills quantity normally."""
    conv = _conv(selected_color="Red", selected_size="40")

    result = extract_order_field(conv, "10", variant_info=VARIANT_INFO)

    assert result == ("pending_order_quantity", 10)


def test_quantity_over_stock_is_reported_invalid():
    """Quantity above available stock returns the invalid marker, not a value."""
    conv = _conv(selected_color="Red", selected_size="40")

    result = extract_order_field(
        conv, "40", variant_info=VARIANT_INFO, available_stock=10
    )

    assert result == ("quantity_invalid", 10)


def test_multislot_quantity_respects_stock_limit():
    """A multi-slot quantity above stock is ignored rather than silently set."""
    conv = _conv()

    result = extract_order_field(
        conv, "red XL 40 pieces", variant_info=VARIANT_INFO, available_stock=10
    )

    assert result == ("selected_color", "Red")
    assert conv.selected_size == "XL"
    assert conv.pending_order_quantity is None
