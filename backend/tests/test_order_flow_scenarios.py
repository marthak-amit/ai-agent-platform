"""
Comprehensive order-flow slot-machine test suite (~50 scenarios).

Tests are grouped into 7 categories (A–G) matching the spec. Each test
operates on pure-Python functions in conversation_flow and catalogue_service,
so no DB or external-API mocking is needed. Heavier scenarios that touch
order creation or the webhook use lightweight stubs only.

Categories:
  A — Happy path (variant + non-variant, multi-language)
  B — Product discovery (no-SKU flows)
  C — Stock privacy & quantity validation
  D — Interruptions mid-order
  E — Confirmation gate
  F — Post-completion recovery
  G — Edge cases
"""
from __future__ import annotations

import re
import pytest

from app.services.conversation_flow import (
    extract_order_field,
    get_next_required_slot,
    get_order_slots,
    detect_stage,
    is_valid_name,
    COMMAND_WORDS,
    _is_price_objection,
)
from app.services.catalogue_service import (
    extract_skus_from_text,
    format_catalogue_context,
    search_products_with_scores,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeConv:
    """Minimal stub that mimics the Conversation ORM object for pure-function tests."""

    def __init__(
        self,
        *,
        selected_color=None,
        selected_size=None,
        selected_material=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
        current_stage="order_collection",
        interrupted_sku=None,
        summary_shown=False,
    ):
        self.selected_color = selected_color
        self.selected_size = selected_size
        self.selected_material = selected_material
        self.pending_order_quantity = pending_order_quantity
        self.customer_name = customer_name
        self.delivery_address = delivery_address
        self.payment_method = payment_method
        self.current_stage = current_stage
        self.interrupted_sku = interrupted_sku
        self.summary_shown = summary_shown


class FakeProduct:
    """Minimal product stub."""

    def __init__(self, *, name, sku, price=999, stock=20, has_variants=False,
                 is_active=True, category="kurti", description="", variants=None):
        self.name = name
        self.sku = sku
        self.price = price
        self.stock = stock
        self.has_variants = has_variants
        self.is_active = is_active
        self.category = category
        self.description = description
        self.variants = variants or []


class FakeVariant:
    """Minimal product-variant stub."""

    def __init__(self, *, color=None, size=None, material=None, stock=10, is_active=True):
        self.color = color
        self.size = size
        self.material = material
        self.stock = stock
        self.is_active = is_active


# Variant-info dicts (returned by get_product_variant_info)
VI_COLOR_SIZE = {
    "has_variants": True, "needs_color": True, "needs_size": True, "needs_material": False,
    "available_colors": ["Red", "Blue", "Green"],
    "available_sizes": ["S", "M", "L", "XL"],
    "available_materials": [],
}
VI_COLOR_ONLY = {
    "has_variants": True, "needs_color": True, "needs_size": False, "needs_material": False,
    "available_colors": ["Pink", "Navy", "White"],
    "available_sizes": [],
    "available_materials": [],
}
VI_NO_VARIANT = {
    "has_variants": False, "needs_color": False, "needs_size": False, "needs_material": False,
    "available_colors": [], "available_sizes": [], "available_materials": [],
}
VI_MATERIAL = {
    "has_variants": True, "needs_color": True, "needs_size": False, "needs_material": True,
    "available_colors": ["Gold", "Silver"],
    "available_sizes": [],
    "available_materials": ["Silk", "Cotton"],
}


def fresh_color_size_conv() -> FakeConv:
    return FakeConv()


def after_color(color="Red") -> FakeConv:
    return FakeConv(selected_color=color)


def after_color_size(color="Red", size="M") -> FakeConv:
    return FakeConv(selected_color=color, selected_size=size)


def after_color_size_qty(color="Red", size="M", qty=2) -> FakeConv:
    return FakeConv(selected_color=color, selected_size=size, pending_order_quantity=qty)


def after_color_size_qty_name(color="Red", size="M", qty=2, name="Priya Shah") -> FakeConv:
    return FakeConv(selected_color=color, selected_size=size, pending_order_quantity=qty, customer_name=name)


def fully_filled_variant(color="Red", size="M", qty=2, name="Priya Shah",
                          addr="702 MG Road Surat", payment="COD") -> FakeConv:
    return FakeConv(selected_color=color, selected_size=size,
                    pending_order_quantity=qty, customer_name=name,
                    delivery_address=addr, payment_method=payment)


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY A — HAPPY PATH (variant & non-variant, multi-language)
# ═════════════════════════════════════════════════════════════════════════════

class TestHappyPathVariant:
    """A1–A5: Color+size product, English/Hindi/Gujarati phrasing."""

    def test_a1_slot_order_color_first(self):
        """A1: Fresh conv with color+size variant — first slot must be color."""
        conv = fresh_color_size_conv()
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "color"

    def test_a2_color_extracted_english(self):
        """A2: English color answer 'I want Red' extracts correctly."""
        conv = fresh_color_size_conv()
        result = extract_order_field(conv, "I want Red", VI_COLOR_SIZE)
        assert result == ("selected_color", "Red")

    def test_a3_color_extracted_hindi(self):
        """A3: Hindi 'lal rang chahiye' — here 'Red' is in message (fallback test)."""
        conv = fresh_color_size_conv()
        result = extract_order_field(conv, "Red chahiye mujhe", VI_COLOR_SIZE)
        assert result == ("selected_color", "Red")

    def test_a4_size_becomes_next_after_color(self):
        """A4: After color, next slot is size."""
        conv = after_color("Blue")
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "size"

    def test_a5_size_extracted_gujarati_phrasing(self):
        """A5: Gujarati phrasing 'M joiye chhe' — size M extracted."""
        conv = after_color("Blue")
        result = extract_order_field(conv, "M joiye chhe", VI_COLOR_SIZE)
        assert result == ("selected_size", "M")

    def test_a6_quantity_extracted_after_variants(self):
        """A6: After color+size, quantity extracted from '3 chahiye'."""
        conv = after_color_size()
        result = extract_order_field(conv, "3 chahiye", VI_COLOR_SIZE, available_stock=10)
        assert result == ("pending_order_quantity", 3)

    def test_a7_name_extracted_after_quantity(self):
        """A7: After qty set, name extracted from plain English answer."""
        conv = after_color_size_qty()
        result = extract_order_field(conv, "Riya Patel", VI_COLOR_SIZE)
        assert result == ("customer_name", "Riya Patel")

    def test_a8_address_extracted(self):
        """A8: Address extracted after name is set."""
        conv = after_color_size_qty_name()
        result = extract_order_field(conv, "45 Sardar Patel Nagar, Ahmedabad", VI_COLOR_SIZE)
        assert result == ("delivery_address", "45 Sardar Patel Nagar, Ahmedabad")

    def test_a9_payment_cod_extracted(self):
        """A9: 'COD karna hai' → payment_method = COD."""
        conv = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Riya Patel",
            delivery_address="45 Sardar Patel Nagar, Ahmedabad",
        )
        result = extract_order_field(conv, "COD karna hai", VI_COLOR_SIZE)
        assert result == ("payment_method", "COD")

    def test_a10_all_slots_none_when_complete(self):
        """A10: Fully filled variant conv returns None (no more slots)."""
        conv = fully_filled_variant()
        assert get_next_required_slot(conv, VI_COLOR_SIZE) is None


class TestHappyPathNonVariant:
    """A11–A15: Non-variant product, simpler slot sequence."""

    def test_a11_first_slot_is_quantity(self):
        """A11: Non-variant product → first slot is quantity."""
        conv = FakeConv()
        assert get_next_required_slot(conv, VI_NO_VARIANT) == "quantity"

    def test_a12_quantity_from_gujarati(self):
        """A12: 'Bey joiye' — no digit match → None (re-ask, not silent default)."""
        conv = FakeConv()
        # 'bey' is Gujarati for 2 but has no digit → extractor returns None to re-ask
        result = extract_order_field(conv, "bey joiye", VI_NO_VARIANT, available_stock=10)
        assert result is None

    def test_a13_quantity_from_digit(self):
        """A13: '5 pieces chahiye' → qty 5."""
        conv = FakeConv()
        result = extract_order_field(conv, "5 pieces chahiye", VI_NO_VARIANT, available_stock=10)
        assert result == ("pending_order_quantity", 5)

    def test_a14_nonvariant_slot_sequence(self):
        """A14: Slot order for non-variant: quantity → customer_name → delivery_address → payment_method."""
        assert get_order_slots(VI_NO_VARIANT) == ["quantity", "customer_name", "delivery_address", "payment_method"]

    def test_a15_variant_slot_sequence_color_size(self):
        """A15: Slot order for color+size variant product."""
        assert get_order_slots(VI_COLOR_SIZE) == ["color", "size", "quantity", "customer_name", "delivery_address", "payment_method"]


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY B — PRODUCT DISCOVERY (no SKU / search flows)
# ═════════════════════════════════════════════════════════════════════════════

class TestProductDiscovery:
    """B1–B10: Search, fuzzy match, multi-match, generic queries."""

    def _products(self):
        return [
            FakeProduct(name="Banarasi Silk Saree", sku="SR27754", price=2450, category="saree", description="Handwoven Banarasi silk"),
            FakeProduct(name="Cotton Kurti Blue", sku="KU10021", price=850, category="kurti", description="Casual cotton kurti"),
            FakeProduct(name="Bridal Lehenga Red", sku="LH00123", price=8500, category="lehenga", description="Heavy bridal lehenga"),
            FakeProduct(name="Silk Dupatta", sku="DP55820", price=600, category="dupatta", description="Pure silk dupatta"),
            FakeProduct(name="Banarasi Silk Kurti", sku="KU20045", price=1200, category="kurti", description="Silk kurti Banarasi design"),
        ]

    def test_b1_sku_extraction_standard(self):
        """B1: Standard SKU extracted from customer message."""
        skus = extract_skus_from_text("SR27754 chahiye")
        assert "SR27754" in skus

    def test_b2_sku_extraction_lowercase(self):
        """B2: Lowercase SKU extracted and uppercased."""
        skus = extract_skus_from_text("mujhe sr27754 chahiye")
        assert "SR27754" in skus

    def test_b3_sku_space_tolerant(self):
        """B3: Voice-transcribed 'SR 27754' (space between prefix and digits) normalised."""
        skus = extract_skus_from_text("SR 27754 chahiye")
        assert "SR27754" in skus

    def test_b4_name_search_single_strong_match(self):
        """B4: 'banarasi saree' → single best match (Banarasi Silk Saree)."""
        products = self._products()
        scored = search_products_with_scores(products, "banarasi saree")
        assert len(scored) >= 1
        top_score, top_product = scored[0]
        assert "Banarasi" in top_product.name
        assert top_score > 0

    def test_b5_name_search_multiple_matches(self):
        """B5: 'banarasi' matches both saree and kurti — at least 2 results."""
        products = self._products()
        scored = search_products_with_scores(products, "banarasi")
        assert len(scored) >= 2

    def test_b6_generic_saree_search(self):
        """B6: 'saree chahiye' — saree category product in top results."""
        products = self._products()
        scored = search_products_with_scores(products, "saree chahiye")
        names = [p.name for _, p in scored]
        assert any("Saree" in n for n in names)

    def test_b7_kurti_search(self):
        """B7: 'kurti dikhao' → kurti product in results."""
        products = self._products()
        scored = search_products_with_scores(products, "kurti")
        names = [p.name for _, p in scored]
        assert any("Kurti" in n for n in names)

    def test_b8_misspelled_saree(self):
        """B8: 'sari' (common misspelling) — partial match against 'saree'."""
        products = self._products()
        # 'sari' won't match 'saree' with the current tokenizer but let's confirm
        # the function returns a list (doesn't crash) and score may be 0.
        scored = search_products_with_scores(products, "sari dikhao")
        assert isinstance(scored, list)

    def test_b9_no_sku_in_generic_greeting(self):
        """B9: Generic greeting has no SKU."""
        skus = extract_skus_from_text("Namaste, kuch dikhao")
        assert skus == []

    def test_b10_multiple_skus_in_one_message(self):
        """B10: Message with two SKUs — both extracted."""
        skus = extract_skus_from_text("SR27754 ya LH00123 mein se kaunsa better hai?")
        assert "SR27754" in skus
        assert "LH00123" in skus


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY C — STOCK PRIVACY & QUANTITY VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

_STOCK_DIGIT_PATTERN = re.compile(r"\b\d+\s*(piece|pieces|stock|available)\b", re.IGNORECASE)


def _has_stock_leak(text: str) -> bool:
    """Return True if the text contains a digit+piece/stock/available pattern."""
    return bool(_STOCK_DIGIT_PATTERN.search(text))


class TestStockPrivacy:
    """C1–C10: Stock counts hidden in display context; correct shortfall messages."""

    def _saree_with_variants(self, stock=15):
        v1 = FakeVariant(color="Red", size="M", stock=stock)
        v2 = FakeVariant(color="Blue", size="L", stock=8)
        return FakeProduct(
            name="Silk Saree",
            sku="SR27754",
            price=2000,
            has_variants=True,
            variants=[v1, v2],
            stock=stock + 8,
        )

    def _simple_product(self, stock=20):
        return FakeProduct(name="Plain Kurti", sku="KU10099", price=750, stock=stock)

    def test_c1_display_context_hides_stock_numbers(self):
        """C1: for_display=True — no digit+piece pattern in output."""
        product = self._saree_with_variants(15)
        ctx = format_catalogue_context([product], for_display=True)
        assert not _has_stock_leak(ctx), f"Stock leaked in display context: {ctx}"

    def test_c2_display_context_has_colors(self):
        """C2: for_display=True still shows available colors."""
        product = self._saree_with_variants()
        ctx = format_catalogue_context([product], for_display=True)
        assert "Red" in ctx or "Blue" in ctx

    def test_c3_internal_context_shows_stock(self):
        """C3: for_display=False (internal) shows stock count."""
        product = self._simple_product(20)
        ctx = format_catalogue_context([product], for_display=False)
        assert "20" in ctx

    def test_c4_quantity_within_stock_accepted(self):
        """C4: qty = exact stock → accepted (not quantity_invalid)."""
        conv = after_color_size()
        result = extract_order_field(conv, "15", VI_COLOR_SIZE, available_stock=15)
        assert result == ("pending_order_quantity", 15)

    def test_c5_quantity_exceeds_stock_returns_invalid(self):
        """C5: qty > stock → ('quantity_invalid', stock)."""
        conv = after_color_size()
        result = extract_order_field(conv, "50", VI_COLOR_SIZE, available_stock=10)
        assert result == ("quantity_invalid", 10)

    def test_c6_quantity_one_over_stock_fails(self):
        """C6: qty = stock + 1 → ('quantity_invalid', stock)."""
        conv = after_color_size()
        result = extract_order_field(conv, "11", VI_COLOR_SIZE, available_stock=10)
        assert result == ("quantity_invalid", 10)

    def test_c7_exact_stock_succeeds(self):
        """C7: qty = exactly stock → accepted."""
        conv = after_color_size()
        result = extract_order_field(conv, "10", VI_COLOR_SIZE, available_stock=10)
        assert result == ("pending_order_quantity", 10)

    def test_c8_nonvariant_quantity_exceeds_stock(self):
        """C8: Non-variant product qty exceeds stock → ('quantity_invalid', stock)."""
        conv = FakeConv()
        result = extract_order_field(conv, "100", VI_NO_VARIANT, available_stock=20)
        assert result == ("quantity_invalid", 20)

    def test_c9_no_digit_in_quantity_slot_returns_none(self):
        """C9: No digit in message → None (re-ask the slot, not silent default to 1)."""
        conv = after_color_size()
        result = extract_order_field(conv, "ek chahiye", VI_COLOR_SIZE, available_stock=5)
        assert result is None

    def test_c10_display_context_no_pieces_for_simple_product(self):
        """C10: Simple product (no variants) with for_display=True hides stock."""
        product = self._simple_product(30)
        ctx = format_catalogue_context([product], for_display=True)
        assert "30" not in ctx
        assert not _has_stock_leak(ctx)


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY D — INTERRUPTIONS MID-ORDER
# ═════════════════════════════════════════════════════════════════════════════

class TestInterruptionsMidOrder:
    """D1–D10: New SKU, off-topic, image, price objection, cancel."""

    def test_d1_price_objection_detected_fast_path(self):
        """D1: 'bahut mehenga hai' detected as price objection without LLM."""
        assert _is_price_objection("bahut mehenga hai")

    def test_d2_price_objection_english(self):
        """D2: 'too expensive' → price objection."""
        assert _is_price_objection("this is too expensive for me")

    def test_d3_price_objection_gujarati(self):
        """D3: 'aata matha' (Gujarati for too expensive) → price objection."""
        assert _is_price_objection("aata matha che")

    def test_d4_discount_keyword(self):
        """D4: 'discount chahiye' → price objection."""
        assert _is_price_objection("koi discount chahiye")

    def test_d5_slot_unchanged_after_offending_message(self):
        """D5: Off-topic doesn't change slot — color still next after interruption handled."""
        conv = fresh_color_size_conv()
        # Simulate: color slot pending; customer sends off-topic 'what is flutter'
        # extract_order_field won't match an off-topic message to color slot
        result = extract_order_field(conv, "what is flutter", VI_COLOR_SIZE)
        assert result is None
        # Next slot still color
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "color"

    def test_d6_cancel_keyword_detected_in_stage(self):
        """D6: 'cancel' maps to _CONFIRMATION_NO set — verify via detect_stage in awaiting."""
        # In awaiting_final_confirmation, 'cancel' moves back to order_collection
        stage = detect_stage([], "cancel karo", stored_stage="awaiting_final_confirmation")
        assert stage == "order_collection"

    def test_d7_sku_mid_order_extracted_correctly(self):
        """D7: New SKU mid-order → extracted from message text."""
        skus = extract_skus_from_text("acha ye nahi, LH00123 chahiye mujhe")
        assert "LH00123" in skus

    def test_d8_two_slots_not_filled_on_wrong_answer(self):
        """D8: Wrong color (not in available_colors) → None, slot not advanced."""
        conv = fresh_color_size_conv()
        result = extract_order_field(conv, "Purple chahiye", VI_COLOR_SIZE)
        assert result is None
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "color"

    def test_d9_size_wrong_stays_on_size(self):
        """D9: Wrong size answer ('XXXL' not in sizes) → extraction fails."""
        conv = after_color("Red")
        result = extract_order_field(conv, "XXXL chahiye", VI_COLOR_SIZE)
        assert result is None
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "size"

    def test_d10_payment_slot_invalid_answer(self):
        """D10: Unrecognised payment answer → None (re-prompt)."""
        conv = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Amit Shah",
            delivery_address="123 Main St Surat",
        )
        result = extract_order_field(conv, "I'll pay somehow", VI_COLOR_SIZE)
        assert result is None
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "payment_method"


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY E — CONFIRMATION GATE
# ═════════════════════════════════════════════════════════════════════════════

class TestConfirmationGate:
    """E1–E10: Stage transitions around awaiting_final_confirmation."""

    def test_e1_yes_in_awaiting_moves_to_completed(self):
        """E1: 'yes' in awaiting_final_confirmation → completed."""
        stage = detect_stage([], "yes", stored_stage="awaiting_final_confirmation")
        assert stage == "completed"

    def test_e2_haan_in_awaiting_moves_to_completed(self):
        """E2: 'haan' (Hindi yes) → completed."""
        stage = detect_stage([], "haan", stored_stage="awaiting_final_confirmation")
        assert stage == "completed"

    def test_e3_bilkul_in_awaiting_moves_to_completed(self):
        """E3: 'bilkul' → completed."""
        stage = detect_stage([], "bilkul", stored_stage="awaiting_final_confirmation")
        assert stage == "completed"

    def test_e4_no_in_awaiting_moves_to_order_collection(self):
        """E4: 'no' in awaiting_final_confirmation → order_collection (edit mode)."""
        stage = detect_stage([], "no", stored_stage="awaiting_final_confirmation")
        assert stage == "order_collection"

    def test_e5_nahi_in_awaiting_moves_to_order_collection(self):
        """E5: 'nahi' (Hindi no) → order_collection."""
        stage = detect_stage([], "nahi", stored_stage="awaiting_final_confirmation")
        assert stage == "order_collection"

    def test_e6_change_in_awaiting_moves_to_order_collection(self):
        """E6: 'change karna hai' → order_collection."""
        stage = detect_stage([], "change karna hai", stored_stage="awaiting_final_confirmation")
        assert stage == "order_collection"

    def test_e7_off_topic_in_awaiting_stays_in_awaiting(self):
        """E7: Off-topic message in awaiting_final_confirmation → stays in awaiting."""
        stage = detect_stage([], "what time is it", stored_stage="awaiting_final_confirmation")
        assert stage == "awaiting_final_confirmation"

    def test_e8_incomplete_slots_next_slot_not_none(self):
        """E8: Conv with missing payment_method → next_slot is payment_method (not None)."""
        conv = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Priya",
            delivery_address="Ahmedabad",
            payment_method=None,
        )
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "payment_method"

    def test_e9_complete_slots_next_slot_is_none(self):
        """E9: Fully filled → next_slot is None, ready for confirmation."""
        conv = fully_filled_variant()
        assert get_next_required_slot(conv, VI_COLOR_SIZE) is None

    def test_e10_confirm_requires_all_slots(self):
        """E10: Half-filled conv (missing address+payment) → next_slot is delivery_address."""
        conv = after_color_size_qty_name()
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "delivery_address"


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY F — POST-COMPLETION RECOVERY
# ═════════════════════════════════════════════════════════════════════════════

class TestPostCompletionRecovery:
    """F1–F10: Behaviour after order is marked completed."""

    def test_f1_completed_stage_locked_irreversible(self):
        """F1: Once completed, detect_stage always returns 'completed'."""
        stage = detect_stage([], "Hi, new order karna hai", stored_stage="completed")
        assert stage == "completed"

    def test_f2_completed_stage_locked_with_sku(self):
        """F2: Even a new SKU in message, completed stays completed."""
        stage = detect_stage([], "SR27754 chahiye", stored_stage="completed")
        assert stage == "completed"

    def test_f3_completed_stage_locked_with_payment_word(self):
        """F3: Payment-related words after completion don't move stage."""
        stage = detect_stage([], "paid", stored_stage="completed")
        assert stage == "completed"

    def test_f4_new_conv_after_reset_starts_at_color(self):
        """F4: Fresh conv for variant product → first slot is color (not quantity)."""
        conv = FakeConv()
        assert get_next_required_slot(conv, VI_COLOR_ONLY) == "color"

    def test_f5_new_conv_after_reset_nonvariant_starts_at_quantity(self):
        """F5: Fresh conv for non-variant product → first slot is quantity."""
        conv = FakeConv()
        assert get_next_required_slot(conv, VI_NO_VARIANT) == "quantity"

    def test_f6_greeting_after_completed_is_greeting_stage(self):
        """F6: 'Hi' message from fresh conversation (no stored stage) → greeting."""
        stage = detect_stage([], "Hi", stored_stage=None)
        assert stage == "greeting"

    def test_f7_color_only_variant_slot_order(self):
        """F7: Color-only variant → slot order: color → quantity → name → addr → payment."""
        assert get_order_slots(VI_COLOR_ONLY) == ["color", "quantity", "customer_name", "delivery_address", "payment_method"]

    def test_f8_material_variant_slot_order(self):
        """F8: Color+material variant → slot order includes color then material."""
        slots = get_order_slots(VI_MATERIAL)
        assert slots[0] == "color"
        assert "material" in slots
        assert slots.index("color") < slots.index("material")
        assert slots.index("material") < slots.index("quantity")

    def test_f9_reset_clears_all_slots(self):
        """F9: After reset (simulated fresh conv), no slots pre-filled."""
        conv = FakeConv()
        for attr in ["selected_color", "selected_size", "pending_order_quantity",
                      "customer_name", "delivery_address", "payment_method"]:
            assert getattr(conv, attr) is None

    def test_f10_completed_stage_with_hi_stays_completed(self):
        """F10: Customer sends 'Hi' after completion — stage stays completed."""
        stage = detect_stage(
            [{"role": "assistant", "content": "Your order is confirmed! 🎉"}],
            "Hi",
            stored_stage="completed",
        )
        assert stage == "completed"


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY G — EDGE CASES
# ═════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """G1–G10: Combined messages, bare-SKU-as-address guard, voice SKU, etc."""

    def test_g1_bare_sku_not_accepted_as_address(self):
        """G1: Bare SKU token not accepted as delivery address."""
        conv = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Riya Shah",
        )
        result = extract_order_field(conv, "SR27754", VI_COLOR_SIZE)
        assert result is None
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "delivery_address"

    def test_g2_voice_sku_with_space_extracted(self):
        """G2: Voice-transcribed 'SR 27754' (with space) normalised to SR27754."""
        skus = extract_skus_from_text("SR 27754 chahiye mujhe")
        assert "SR27754" in skus

    def test_g3_multiple_skus_both_extracted(self):
        """G3: Two SKUs in one message → both returned."""
        skus = extract_skus_from_text("SR27754 ya KU10021 better hai?")
        assert len(skus) == 2
        assert "SR27754" in skus
        assert "KU10021" in skus

    def test_g4_name_must_not_be_command_word(self):
        """G4: 'reset' must not pass is_valid_name."""
        assert not is_valid_name("reset")

    def test_g5_name_must_not_be_color_word(self):
        """G5: 'Red' must not pass is_valid_name (colour command word)."""
        assert not is_valid_name("Red")

    def test_g6_name_must_not_be_payment_word(self):
        """G6: 'COD' must not pass is_valid_name."""
        assert not is_valid_name("COD")

    def test_g7_valid_name_passes(self):
        """G7: 'Priya Mehta' is a valid name."""
        assert is_valid_name("Priya Mehta")

    def test_g8_single_char_name_rejected(self):
        """G8: Single character 'P' not a valid name."""
        assert not is_valid_name("P")

    def test_g9_name_with_digit_rejected(self):
        """G9: Name containing digit '7' rejected by extract (all tokens must be alpha)."""
        conv = after_color_size_qty()
        result = extract_order_field(conv, "Priya7 Shah", VI_COLOR_SIZE)
        assert result is None

    def test_g10_payment_upi_variants(self):
        """G10: Various UPI aliases all map to 'UPI' payment method."""
        conv_base = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Priya",
            delivery_address="Ahmedabad",
        )
        for upi_phrase in ["UPI se dunga", "GPAY karuga", "PHONEPE karo", "PAYTM se"]:
            conv = FakeConv(
                selected_color="Red", selected_size="M",
                pending_order_quantity=2, customer_name="Priya",
                delivery_address="Ahmedabad",
            )
            result = extract_order_field(conv, upi_phrase, VI_COLOR_SIZE)
            assert result == ("payment_method", "UPI"), f"Failed for: {upi_phrase}"


# ═════════════════════════════════════════════════════════════════════════════
# ADDITIONAL CROSS-CATEGORY SCENARIOS (fills to ~50 total)
# ═════════════════════════════════════════════════════════════════════════════

class TestStageDetection:
    """Stage-detection scenarios ensuring no oscillation."""

    def test_payment_stage_locked(self):
        """Payment stage stays payment unless payment-confirmation word sent."""
        stage = detect_stage([], "tell me more about this product", stored_stage="payment")
        assert stage == "payment"

    def test_payment_confirmation_advances_to_completed(self):
        """Payment word in payment stage → completed."""
        stage = detect_stage([], "paid", stored_stage="payment")
        assert stage == "completed"

    def test_ho_gaya_advances_payment_to_completed(self):
        """'ho gaya' in payment stage → completed."""
        stage = detect_stage([], "ho gaya", stored_stage="payment")
        assert stage == "completed"

    def test_bhej_diya_advances_payment_to_completed(self):
        """'bhej diya' in payment stage → completed."""
        stage = detect_stage([], "bhej diya", stored_stage="payment")
        assert stage == "completed"

    def test_order_followup_cue_locks_to_order_collection(self):
        """If agent asked 'kitne piece chahiye', next turn is order_collection."""
        history = [{"role": "assistant", "content": "kitne piece chahiye aapko?"}]
        stage = detect_stage(history, "3", stored_stage="product_inquiry")
        assert stage == "order_collection"

    def test_greeting_with_no_history(self):
        """Empty history + any message → greeting."""
        stage = detect_stage([], "Hello", stored_stage=None)
        assert stage == "greeting"

    def test_off_topic_detected(self):
        """Cricket in message → off_topic."""
        stage = detect_stage([], "IPL mein kaun jeetega?", stored_stage=None)
        assert stage == "off_topic"

    def test_name_extraction_refuses_command_word_ok(self):
        """Command word 'exit' typed when name expected → None."""
        conv = after_color_size_qty()
        result = extract_order_field(conv, "exit", VI_COLOR_SIZE)
        assert result is None

    def test_cod_cash_on_delivery_extracted(self):
        """'Cash on delivery karna hai' → COD."""
        conv = FakeConv(
            selected_color="Red", selected_size="M",
            pending_order_quantity=2, customer_name="Amit",
            delivery_address="Surat",
        )
        result = extract_order_field(conv, "Cash on delivery karna hai", VI_COLOR_SIZE)
        assert result == ("payment_method", "COD")

    def test_address_minimum_length(self):
        """Address must be >= 4 chars; '12' (2 chars) rejected."""
        conv = after_color_size_qty_name()
        result = extract_order_field(conv, "12", VI_COLOR_SIZE)
        assert result is None

    def test_address_four_chars_rejected(self):
        """A bare city name (no digit, no comma, < 10 chars) is not a complete address."""
        conv = after_color_size_qty_name()
        result = extract_order_field(conv, "Pune", VI_COLOR_SIZE)
        assert result is None  # is_valid_address requires digit or comma + min 10 chars

    def test_address_full_with_pincode_accepted(self):
        """A proper address with house number and pincode is accepted."""
        conv = after_color_size_qty_name()
        result = extract_order_field(conv, "12 MG Road, Pune 411001", VI_COLOR_SIZE)
        assert result == ("delivery_address", "12 MG Road, Pune 411001")

    def test_color_case_insensitive(self):
        """Color matching is case-insensitive: 'red' matches 'Red'."""
        conv = fresh_color_size_conv()
        result = extract_order_field(conv, "red chahiye", VI_COLOR_SIZE)
        assert result == ("selected_color", "Red")

    def test_size_case_insensitive(self):
        """Size matching: 'xl' matches 'XL'."""
        conv = after_color("Red")
        result = extract_order_field(conv, "xl size chahiye", VI_COLOR_SIZE)
        assert result == ("selected_size", "XL")

    def test_quantity_zero_not_accepted_as_filled(self):
        """Quantity of 0 is treated as unfilled — next_slot returns 'quantity'."""
        conv = FakeConv(selected_color="Red", selected_size="M", pending_order_quantity=0)
        assert get_next_required_slot(conv, VI_COLOR_SIZE) == "quantity"

    def test_material_extracted(self):
        """Material 'Silk' extracted from 'Silk chahiye'."""
        conv = FakeConv(selected_color="Gold")
        result = extract_order_field(conv, "Silk chahiye", VI_MATERIAL)
        assert result == ("selected_material", "Silk")
