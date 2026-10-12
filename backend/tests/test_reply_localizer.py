"""Unit tests for reply_localizer: translate only when the script is wrong, never lose a fact."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import reply_localizer as rl
from app.services.language_service import reply_matches_language
from app.services.order_pipeline import ButtonSpec, PipelineResult


@pytest.fixture(autouse=True)
def _clear_cache():
    """The translation cache is a module global."""
    rl._cache.clear()
    yield
    rl._cache.clear()


def _fake_llm(monkeypatch, answer: str | Exception):
    """Install a fake llm_call returning `answer` (or raising it)."""
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer if isinstance(answer, str) else ""))])
    mock = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=resp)
    monkeypatch.setattr("app.services.llm_client.llm_call", mock)
    return mock


async def test_reply_in_right_script_is_untouched_and_costs_nothing(monkeypatch):
    """A Gujarati reply to a Gujarati customer never reaches the LLM."""
    llm = _fake_llm(monkeypatch, "x")
    text = "હા, અમે ચણિયા ચોળી વેચીએ છીએ."
    assert await rl.localize(text, "gujarati_script") == text
    llm.assert_not_called()


async def test_english_reply_to_tamil_customer_is_translated_and_cached(monkeypatch):
    """English text for a Tamil customer is translated once; the second call is a cache hit."""
    llm = _fake_llm(monkeypatch, "இந்த புடவை ₹899 ஆகும். SR20001")
    src = "This saree costs ₹899. SR20001"
    out = await rl.localize(src, "tamil")
    assert out == "இந்த புடவை ₹899 ஆகும். SR20001" and reply_matches_language(out, "tamil")
    assert await rl.localize(src, "tamil") == out
    assert llm.await_count == 1


async def test_translation_that_drops_a_price_is_rejected(monkeypatch):
    """A translation missing ₹899 / the SKU falls back to the original text — never a wrong fact."""
    _fake_llm(monkeypatch, "இந்த புடவை விலை அதிகம்")
    src = "This saree costs ₹899. SR20001"
    assert await rl.localize(src, "tamil") == src


async def test_translation_in_wrong_script_is_rejected(monkeypatch):
    """Output that is still English is not accepted as a translation."""
    _fake_llm(monkeypatch, "This saree costs ₹899. SR20001 again")
    src = "This saree costs ₹899. SR20001"
    assert await rl.localize(src, "bengali") == src


async def test_llm_failure_falls_back_to_original(monkeypatch):
    """Rate limit / outage never blocks the reply."""
    _fake_llm(monkeypatch, RuntimeError("429 rate limited"))
    assert await rl.localize("Order confirmed", "marathi") == "Order confirmed"


async def test_english_customer_is_never_translated(monkeypatch):
    """No LLM call for English customers, even if the text has an Indic product name."""
    llm = _fake_llm(monkeypatch, "x")
    assert await rl.localize("Do you want साड़ी?", "english") == "Do you want साड़ी?"
    llm.assert_not_called()


async def test_localize_result_translates_text_and_static_buttons_only(monkeypatch):
    """Body + fixed buttons are translated; product/colour buttons keep their catalogue spelling."""
    answers = {"Confirm": "உறுதி", "Pick one": "ஒன்றைத் தேர்வு செய்க"}

    async def fake(purpose, model, messages, *a, **k):
        """Translate by lookup."""
        src = messages[-1]["content"]
        content = answers.get(src, "தமிழ் " + src)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr("app.services.llm_client.llm_call", fake)
    result = PipelineResult(text="Pick one", buttons=[ButtonSpec("confirm_pay", "Confirm"), ButtonSpec("SR20001", "Green Saree")])
    out = await rl.localize_result(result, "tamil")
    assert out.text == "ஒன்றைத் தேர்வு செய்க"
    assert out.buttons[0].title == "உறுதி" and out.buttons[1].title == "Green Saree"


def test_reply_matches_language_rules():
    """Native languages need native letters; Latin languages reject Indic script; facts are ignored."""
    assert reply_matches_language("હા, ₹500 છે. SR20001", "gujarati_script")
    assert not reply_matches_language("Yes, it is available", "gujarati_script")
    assert not reply_matches_language("हाँ जी, उपलब्ध है", "hindi_roman")
    assert reply_matches_language("Haan ji, available hai", "hindi_roman")
    assert reply_matches_language("😊 500", "tamil")


def test_stray_latin_words_ignore_brands_ids_and_allowed_names():
    """English prose is flagged in a native-script reply; brands, SKUs, URLs and catalogue names are not."""
    text = "માફ કરશો, size માં red ઉપલબ્ધ નથી. UPI shop@upi SR20001 https://x.in/shop Cotton Saree"
    allowed = rl.allowed_words_from("Cotton Saree", "Red")
    assert rl.stray_latin_words(text, allowed) == ["size"]


async def test_mixed_reply_is_translated_even_when_mostly_native(monkeypatch):
    """A Gujarati reply with a leftover English word is not 'pure' → it is sent for translation."""
    llm = _fake_llm(monkeypatch, "માફ કરશો, સાઈઝ M ઉપલબ્ધ નથી")
    out = await rl.localize("માફ કરશો, size M ઉપલબ્ધ નથી", "gujarati_script")
    assert out == "માફ કરશો, સાઈઝ M ઉપલબ્ધ નથી"
    assert llm.await_count == 1


async def test_catalogue_names_do_not_trigger_translation(monkeypatch):
    """Product names written in English by the seller are legitimate inside a Gujarati reply."""
    llm = _fake_llm(monkeypatch, "x")
    text = "📦 Printed Kurti × 1 = ₹450 ડિલિવરી 3–7 દિવસમાં"
    assert await rl.localize(text, "gujarati_script", allowed=rl.allowed_words_from("Printed Kurti")) == text
    llm.assert_not_called()


async def test_translation_with_more_english_than_the_original_is_rejected(monkeypatch):
    """A 'translation' that adds English words is worse than the original and is dropped."""
    _fake_llm(monkeypatch, "માફ કરશો, size color M ઉપલબ્ધ નથી")
    src = "માફ કરશો, size M ઉપલબ્ધ નથી"
    assert await rl.localize(src, "gujarati_script") == src


async def test_english_reply_to_hinglish_customer_is_rewritten(monkeypatch):
    """Latin-script replies are held to the customer's language too (English text → Hinglish)."""
    llm = _fake_llm(monkeypatch, "Hamari team jald hi aapki madad karegi. Kripya address ke liye haan likhein.")
    out = await rl.localize("Our team will assist you shortly. Please reply yes to use this address", "hindi_roman")
    assert out.startswith("Hamari team")
    assert llm.await_count == 1
    system = llm.await_args.args[2][0]["content"]
    assert "Latin" in system and "Hindi" in system        # the Roman-script prompt variant


async def test_hinglish_reply_to_english_customer_is_rewritten(monkeypatch):
    """A leftover Hinglish string must not reach an English customer."""
    _fake_llm(monkeypatch, "Yes, we have this available. Which colour would you like to order?")
    out = await rl.localize("Haan ji, hamare paas yeh available hai. Kaunsa color chahiye aapko?", "english")
    assert out.startswith("Yes, we have")
