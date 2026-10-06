"""Unit tests for the pure helpers behind the pre-catalog router (see order_pipeline.py)."""

from types import SimpleNamespace

import pytest

from app.services.order_pipeline import (
    _find_exact_name_match,
    _is_exact_sku_message,
    _normalize_name,
    is_order_status_intent,
)


def _p(name, sku="X1", active=True):
    return SimpleNamespace(name=name, sku=sku, is_active=active)


@pytest.mark.parametrize("text", [
    "what is the status of my order?", "Where is my order", "order status", "track my order",
    "mera order kaha hai", "order kahan hai", "meri order ka status", "ORD-2026-12",
    "status of ord-2025-7", "मेरा ऑर्डर कहाँ है", "ऑर्डर स्टेटस", "મારો ઓર્ડર ક્યાં છે", "ઓર્ડર સ્ટેટસ",
])
def test_is_order_status_intent_true(text):
    """Status phrasing in EN/Hinglish/Hindi/Gujarati and ORD-ids are recognised."""
    assert is_order_status_intent(text)


@pytest.mark.parametrize("text", [
    "", "hi", "I want to order this kurti", "stock status of kurti", "status", "what is the price",
    "KU76326", "order",
])
def test_is_order_status_intent_false(text):
    """Bare 'status'/'order' and product questions must not be read as order-status queries."""
    assert not is_order_status_intent(text)


def test_normalize_name():
    """Lower-cases, drops apostrophes, turns punctuation into spaces, collapses whitespace."""
    assert _normalize_name("  Kurti   NEW-One! ") == "kurti new one"
    assert _normalize_name("Men's Kurta") == "mens kurta"
    assert _normalize_name("") == ""


def test_find_exact_name_match_unique_hit():
    """An exact normalized name wins even though other products share words with it."""
    prods = [_p("Kurti New One", "A"), _p("Kurti Cotton", "B")]
    assert _find_exact_name_match(prods, "kurti new one.").sku == "A"


def test_find_exact_name_match_ambiguous_or_missing_is_none():
    """Duplicate names, partial names and inactive products never count as an exact match."""
    assert _find_exact_name_match([_p("Kurti", "A"), _p("kurti", "B")], "Kurti") is None
    assert _find_exact_name_match([_p("Kurti New One")], "kurti new") is None
    assert _find_exact_name_match([_p("Kurti New One", active=False)], "Kurti New One") is None
    assert _find_exact_name_match([_p("Kurti")], "  ") is None


def test_is_exact_sku_message():
    """Whole-message SKU match, case/space/punctuation-insensitive; a sentence containing it is not exact."""
    p = _p("Lehenga", "PR10983")
    assert _is_exact_sku_message(" pr10983 ", p)
    assert _is_exact_sku_message("PR 10983.", p)
    assert not _is_exact_sku_message("is PR10983 available?", p)
    assert not _is_exact_sku_message("PR10984", p)
