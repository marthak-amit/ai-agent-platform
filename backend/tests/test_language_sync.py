"""Per-turn language sync + shared product-card / order-offer helpers (no database)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import order_pipeline as op


def _conv(lang="english", stage="greeting", name=None, address=None):
    """A conversation stand-in with the fields the sync reads."""
    return SimpleNamespace(id=1, last_customer_language=lang, current_stage=stage, customer_name=name, delivery_address=address)


@pytest.fixture
def saved(monkeypatch):
    """Capture conversation_service.update_language calls."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.order_pipeline.conversation_service.update_language", mock)
    return mock


async def test_first_gujarati_message_switches_language_immediately(saved):
    """The very first Gujarati-script message sets the language BEFORE any template is rendered."""
    conv = _conv("english")
    out = await op.sync_conversation_language(None, conv, SimpleNamespace(type="text"), "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?")
    assert out == "gujarati_script" and conv.last_customer_language == "gujarati_script"
    saved.assert_awaited_once_with(None, 1, "gujarati_script")


@pytest.mark.parametrize("text", ["ok", "2", "COD", "Rajesh Patel", "12 MG Road, Pune 411001", "🙏"])
async def test_signal_free_messages_keep_the_language(saved, text):
    """Names, addresses, digits and one-word replies never flip an established language."""
    conv = _conv("gujarati_script")
    assert await op.sync_conversation_language(None, conv, SimpleNamespace(type="text"), text) == "gujarati_script"
    saved.assert_not_awaited()


async def test_button_taps_do_not_change_language(saved):
    """A tap arrives as canonical English text ('confirm') — not evidence of the customer's language."""
    conv = _conv("hindi_devanagari")
    assert await op.sync_conversation_language(None, conv, SimpleNamespace(type="interactive"), "what is the price") == "hindi_devanagari"
    saved.assert_not_awaited()


async def test_roman_text_while_collecting_name_does_not_switch(saved):
    """While the name/address is being collected only a different SCRIPT switches (a Roman street name must not)."""
    conv = _conv("gujarati_script", stage="order_collection")
    out = await op.sync_conversation_language(None, conv, SimpleNamespace(type="text"), "mera naam kya hai bhai kahan")
    assert out == "gujarati_script"
    out = await op.sync_conversation_language(None, conv, SimpleNamespace(type="text"), "मुझे साड़ी चाहिए")
    assert out == "hindi_devanagari"


async def test_db_failure_never_blocks_the_reply(monkeypatch):
    """A failing language write is logged and the stored language is returned unchanged."""
    monkeypatch.setattr("app.services.order_pipeline.conversation_service.update_language", AsyncMock(side_effect=RuntimeError("db")))
    conv = _conv("english")
    assert await op.sync_conversation_language(None, conv, SimpleNamespace(type="text"), "ભાવ શું છે") == "english"


def test_render_product_card_is_translated_but_facts_are_the_databases():
    """Labels follow the language; name, SKU, price and colours come straight from the row."""
    product = SimpleNamespace(name="Navratri Chaniya Choli", sku="CC40001", price=1800.0)
    vi = {"available_colors": ["Green", "Pink"], "available_sizes": ["M"]}
    gu = op.render_product_card("gujarati_script", product, vi)
    assert "Navratri Chaniya Choli [CC40001] — ₹1,800" in gu
    assert "ઉપલબ્ધ રંગ: Green, Pink" in gu and "શું તમે ઓર્ડર કરવા માંગો છો?" in gu and "Would" not in gu
    en = op.render_product_card("english", product, vi, intro=True)
    assert en.startswith("Yes, we have this available:") and en.endswith("Would you like to order? (Yes / No)")
    assert "Would you like" not in op.render_product_card("english", product, vi, ask_order=False)


@pytest.mark.parametrize("text", [
    "Printed Kurti [KU1]\n\nWould you like to order? (Yes / No)", "x\n\nશું તમે ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "x\n\nक्या आप ऑर्डर करना चाहेंगे? (हाँ / नहीं)", "x\n\nKya aap order karna chahenge? (Haan / Nahi)",
])
def test_order_offer_is_recognised_in_every_template_language(text):
    """The Yes/No buttons are attached to the offer question whatever language it is written in."""
    assert op._asks_to_order(text)


def test_order_offer_not_triggered_by_ordinary_text():
    """Normal sentences never grow Yes/No buttons."""
    assert not op._asks_to_order("Colour? Blue / Pink")
