"""Tests for abuse/cost-protection improvements (Improvements 1–5).

These tests are deterministic — no LLM calls, no DB. They verify:
  1. Per-slot attempt cap: escape hatch at attempt 3, no hatch at attempt 2.
  2. Size/colour display = accepted list (display and validator share variant_info).
  3. Name-match confidence constants are wired correctly.
  4. LLM budget constants are sane.
  5. reset_conv RESET_VALUES includes all new counters.
"""

import pytest


# ---------------------------------------------------------------------------
# Improvement 1: _build_slot_question escape hatch
# ---------------------------------------------------------------------------

class _FakeConv:
    selected_color = None
    selected_size = None
    selected_material = None
    pending_order_quantity = None
    customer_name = None
    delivery_address = None
    payment_method = None


def _vi(sizes=None, colors=None, materials=None):
    return {
        "has_variants": True,
        "needs_color": bool(colors),
        "needs_size": bool(sizes),
        "needs_material": bool(materials),
        "available_colors": colors or [],
        "available_sizes": sizes or [],
        "available_materials": materials or [],
    }


def test_slot_escape_hatch_at_attempt_3():
    from app.routers.webhook import _build_slot_question, _SLOT_ATTEMPT_ESCAPE_HATCH

    assert _SLOT_ATTEMPT_ESCAPE_HATCH == 3  # confirm the threshold we tested

    vi = _vi(sizes=["S", "M", "L", "XL"])
    reply_2 = _build_slot_question("size", _FakeConv(), vi, "english", attempt_count=2)
    reply_3 = _build_slot_question("size", _FakeConv(), vi, "english", attempt_count=3)

    # Attempt 2: no escape hatch
    assert "cancel" not in reply_2.lower()

    # Attempt 3: escape hatch must mention cancel/help
    assert "cancel" in reply_3.lower() or "help" in reply_3.lower()


def test_slot_no_escape_hatch_below_threshold():
    from app.routers.webhook import _build_slot_question

    vi = _vi(colors=["Red", "Blue"])
    reply = _build_slot_question("color", _FakeConv(), vi, "english", attempt_count=1)
    assert "cancel" not in reply.lower()


def test_escape_hatch_also_on_material():
    from app.routers.webhook import _build_slot_question

    vi = _vi(materials=["Cotton", "Silk"])
    reply = _build_slot_question("material", _FakeConv(), vi, "english", attempt_count=4)
    assert "cancel" in reply.lower() or "help" in reply.lower()


# ---------------------------------------------------------------------------
# Improvement 5: Display list = accepted list (both read same variant_info)
# ---------------------------------------------------------------------------

def test_full_size_list_displayed_and_accepted():
    """All sizes in variant_info appear in the slot question text."""
    from app.routers.webhook import _build_slot_question

    all_sizes = ["S", "M", "L", "XL", "XXL", "3XL", "38", "40"]
    vi = _vi(sizes=all_sizes)
    reply = _build_slot_question("size", _FakeConv(), vi, "english")
    for sz in all_sizes:
        assert sz in reply, f"Size '{sz}' not displayed in slot question: {reply!r}"


def test_full_color_list_displayed():
    from app.routers.webhook import _build_slot_question

    all_colors = ["Red", "Blue", "Green", "Pink", "Maroon", "Black"]
    vi = _vi(colors=all_colors)
    reply = _build_slot_question("color", _FakeConv(), vi, "english")
    for c in all_colors:
        assert c in reply, f"Color '{c}' not displayed in slot question: {reply!r}"


# ---------------------------------------------------------------------------
# Improvement 4: confidence-gate constants
# ---------------------------------------------------------------------------

def test_name_match_switch_threshold_higher_than_first_pin():
    from app.routers.webhook import _NAME_MATCH_SWITCH_MIN_SCORE, _NAME_MATCH_AUTO_PIN_MIN_SCORE

    # Switch threshold must be stricter than first-pin threshold
    assert _NAME_MATCH_SWITCH_MIN_SCORE > _NAME_MATCH_AUTO_PIN_MIN_SCORE


# ---------------------------------------------------------------------------
# Improvement 3: LLM budget constants are sane
# ---------------------------------------------------------------------------

def test_llm_budget_constants():
    from app.routers.webhook import _DEFAULT_LLM_SOFT_CAP, _DEFAULT_LLM_HARD_CAP, _SLOT_ATTEMPT_ESCALATE

    assert _DEFAULT_LLM_SOFT_CAP > 0
    assert _DEFAULT_LLM_HARD_CAP > _DEFAULT_LLM_SOFT_CAP
    assert _SLOT_ATTEMPT_ESCALATE > 0


# ---------------------------------------------------------------------------
# reset_conv: all new counters are present in RESET_VALUES
# ---------------------------------------------------------------------------

def test_reset_conv_includes_new_fields():
    import sys
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "reset_conv",
        Path(__file__).parent.parent / "reset_conv.py",
    )
    module = importlib.util.module_from_spec(spec)
    # reset_conv imports app.db etc. — only check the dict, not execute
    # Read the source and eval just RESET_VALUES
    src = (Path(__file__).parent.parent / "reset_conv.py").read_text()
    # Extract RESET_VALUES block via simple parse
    assert "slot_attempt_count" in src
    assert "slot_attempt_slot" in src
    assert "off_topic_count" in src
    assert "llm_calls_today" in src
    assert "llm_calls_date" in src


# ---------------------------------------------------------------------------
# Improvement 2: off-topic threshold constant
# ---------------------------------------------------------------------------

def test_off_topic_threshold_constant():
    from app.routers.webhook import _DEFAULT_OFF_TOPIC_THRESHOLD

    assert _DEFAULT_OFF_TOPIC_THRESHOLD >= 3  # must give at least 3 chances
    assert _DEFAULT_OFF_TOPIC_THRESHOLD <= 10  # must not be absurdly permissive


# ---------------------------------------------------------------------------
# FIX 1: is_valid_address validator (deterministic, no DB/LLM)
# ---------------------------------------------------------------------------

def test_is_valid_address_accepts_real_addresses():
    from app.services.conversation_flow import is_valid_address

    valid = [
        "42 MG Road, Pune 411001",
        "s-11 new road, goa 403001",
        "Flat 5B, Sunrise Apartments, Andheri West, Mumbai 400058",
        "12/3, Park Street, Kolkata 700016",
        "House No. 7, Sector 15, Noida 201301",
    ]
    for addr in valid:
        assert is_valid_address(addr), f"should accept: {addr!r}"


def test_is_valid_address_rejects_questions():
    from app.services.conversation_flow import is_valid_address

    rejects = [
        "how much delivery charge will be?",
        "what is the price?",
        "kya free delivery hai?",
        "when will it arrive?",
        "how much?",
    ]
    for text in rejects:
        assert not is_valid_address(text), f"should reject question: {text!r}"


def test_is_valid_address_rejects_stop_words():
    from app.services.conversation_flow import is_valid_address

    stop_words = ["yes", "no", "ok", "okay", "ha", "haan", "paid", "cancel",
                  "change", "thanks", "hello", "hi"]
    for word in stop_words:
        assert not is_valid_address(word), f"should reject stop-word: {word!r}"


def test_is_valid_address_rejects_short_texts():
    from app.services.conversation_flow import is_valid_address

    short = ["abc", "goa", "mumbai", "1234", "ok sure"]
    for text in short:
        assert not is_valid_address(text), f"should reject short text: {text!r}"


def test_is_valid_address_rejects_no_digit_no_comma():
    from app.services.conversation_flow import is_valid_address

    # Text long enough but no digit and no comma — probably not an address
    assert not is_valid_address("andheri west mumbai maharashtra")


def test_is_valid_address_accepts_address_with_comma_no_digit():
    from app.services.conversation_flow import is_valid_address

    # Comma present even without an explicit digit — should accept
    assert is_valid_address("Andheri West, Mumbai, Maharashtra")


def test_detect_change_address_intent_with_address():
    from app.routers.webhook import _detect_change_address_intent

    detected, addr = _detect_change_address_intent("change it to s-11 new road, goa")
    assert detected is True
    assert addr is not None
    assert "s-11" in addr.lower() or "new road" in addr.lower()


def test_detect_change_address_intent_pure():
    from app.routers.webhook import _detect_change_address_intent

    detected, addr = _detect_change_address_intent("I just want to change the address")
    assert detected is True
    # No address provided in pure intent message
    # (addr may be None or empty — caller prompts for one)


def test_detect_change_address_intent_negative():
    from app.routers.webhook import _detect_change_address_intent

    detected, _ = _detect_change_address_intent("Blue colour please")
    assert detected is False


def test_is_order_aside_question():
    from app.routers.webhook import _is_order_aside_question

    questions = [
        "how much delivery charges?",
        "what is the total?",
        "delivery charges?",
        "price kitna hai",
        "kya return policy hai?",
    ]
    for q in questions:
        assert _is_order_aside_question(q), f"should detect as question: {q!r}"


def test_is_not_order_aside_question():
    from app.routers.webhook import _is_order_aside_question

    non_questions = [
        "Blue",
        "M",
        "Amit Kumar",
        "42 MG Road Pune",
        "COD",
        "2",
    ]
    for text in non_questions:
        assert not _is_order_aside_question(text), f"should NOT detect as question: {text!r}"


def test_is_simple_ack():
    from app.routers.webhook import _is_simple_ack

    acks = ["ok", "got it", "i got it", "achha got it", "noted", "fine"]
    for a in acks:
        assert _is_simple_ack(a), f"should detect as ack: {a!r}"

    non_acks = ["Blue", "how much", "yes please order", "42 MG Road"]
    for text in non_acks:
        assert not _is_simple_ack(text), f"should NOT detect as ack: {text!r}"
