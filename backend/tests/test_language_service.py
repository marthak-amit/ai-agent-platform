"""
Unit tests for language_service.detect_language() and get_language_instruction().

Each test covers one scenario, matching the docstring contract.
Run with:  pytest tests/test_language_service.py -v
"""

import pytest

from app.services.language_service import (
    build_language_rule,
    detect_language,
    get_language_instruction,
)


# ── detect_language ───────────────────────────────────────────────────────────


def test_detect_english_product_query():
    """Plain English product question → english."""
    assert detect_language("banarasi saree price") == "english"


def test_detect_english_greeting():
    """Short English greeting → english (≤3 words + ASCII)."""
    assert detect_language("hello") == "english"


def test_detect_english_availability():
    """English stock question → english."""
    assert detect_language("is this available?") == "english"


def test_detect_hindi_devanagari():
    """Pure Devanagari message → hindi_devanagari."""
    assert detect_language("बनारसी साड़ी का दाम क्या है") == "hindi_devanagari"


def test_detect_hindi_devanagari_threshold():
    """Fewer than 3 Devanagari chars should NOT trigger hindi_devanagari."""
    result = detect_language("hi ₹ ok")  # no Devanagari at all
    assert result != "hindi_devanagari"


def test_detect_hindi_roman_kya_hai():
    """Classic Hinglish with 'kya hai' → hindi_roman."""
    assert detect_language("banarasi saree ka price kya hai") == "hindi_roman"


def test_detect_hindi_roman_chahiye():
    """'mujhe chahiye' keyword → hindi_roman."""
    assert detect_language("mujhe ek saree chahiye") == "hindi_roman"


def test_detect_gujarati_roman_kem_cho():
    """'kem cho' keyword → gujarati_roman."""
    assert detect_language("kem cho, banarasi saree ni kimat ketlu che") == "gujarati_roman"


def test_detect_gujarati_roman_chhe():
    """'chhe' keyword alone → gujarati_roman."""
    assert detect_language("aa product stock mein chhe") == "gujarati_roman"


def test_detect_gujarati_script():
    """Gujarati-script message → gujarati_script."""
    assert detect_language("સાડીની કિંમત શું છે") == "gujarati_script"


def test_detect_hinglish_default():
    """Non-ASCII message with no matching keywords → hindi_roman (Hinglish is merged into hindi_roman).

    "₹500 main chahun" uses ₹ (non-ASCII) so the English path is skipped,
    and neither the Gujarati nor Hindi keyword lists match.
    "hinglish" is no longer a distinct return value — mixed Hindi-English maps to "hindi_roman".
    """
    assert detect_language("₹500 main chahun") == "hindi_roman"


def test_detect_empty_string():
    """Empty string should return some valid language without crashing."""
    result = detect_language("")
    assert result in {
        "english", "hindi_devanagari", "hindi_roman",
        "gujarati_script", "gujarati_roman", "hinglish",
    }


# ── get_language_instruction ──────────────────────────────────────────────────


@pytest.mark.parametrize("lang,expected_fragment", [
    ("english",           "ENGLISH ONLY"),
    ("hindi_devanagari",  "Devanagari"),
    ("hindi_roman",       "Hinglish"),
    ("gujarati_script",   "Gujarati script"),
    ("gujarati_roman",    "Gujarati written in Roman letters"),
    ("hinglish",          "Hinglish"),
])
def test_get_language_instruction_contains_key_phrase(lang: str, expected_fragment: str):
    """Each language code must produce an instruction mentioning the key phrase."""
    instruction = get_language_instruction(lang)
    assert expected_fragment in instruction, (
        f"Expected '{expected_fragment}' in instruction for lang='{lang}'"
    )


def test_get_language_instruction_unknown_lang_returns_english():
    """An unknown language code is treated as English (same fallback the templates use)."""
    result = get_language_instruction("klingon")
    assert "ENGLISH" in result


# ── build_language_rule ───────────────────────────────────────────────────────


def test_build_language_rule_starts_with_warning():
    """The critical-rule block must open with the warning emoji."""
    rule = build_language_rule("english")
    assert rule.startswith("⚠️ CRITICAL LANGUAGE RULE")


def test_build_language_rule_contains_detected_lang():
    """The rule must echo back the detected language code."""
    rule = build_language_rule("hindi_roman")
    assert "hindi_roman" in rule


def test_build_language_rule_english_says_english():
    """English rule must instruct the model to reply in ENGLISH."""
    rule = build_language_rule("english")
    assert "ENGLISH" in rule


def test_build_language_rule_gujarati_script():
    """Gujarati script rule must mention Gujarati."""
    rule = build_language_rule("gujarati_script")
    assert "GUJARATI" in rule


# ── multilingual detection (any script) ───────────────────────────────────────

from app.services.language_service import (  # noqa: E402
    detect,
    expected_script,
    is_ambiguous,
    normalize_language,
    script_counts,
)


@pytest.mark.parametrize("text,expected", [
    ("શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?", "gujarati_script"),
    ("હા", "gujarati_script"),
    ("આ સાડી નો price શું છે", "gujarati_script"),
    ("इस साड़ी की कीमत क्या है", "hindi_devanagari"),
    ("तुमच्याकडे साडी आहे का?", "marathi"),
    ("मला साडी पाहिजे", "marathi"),
    ("এই শাড়ির দাম কত?", "bengali"),
    ("இந்த புடவை விலை என்ன?", "tamil"),
    ("ఈ చీర ధర ఎంత?", "telugu"),
    ("ನಿಮ್ಮ ಬಳಿ ಸೀರೆ ಇದೆಯೇ", "kannada"),
    ("നിങ്ങളുടെ പക്കൽ സാരി ഉണ്ടോ", "malayalam"),
    ("ਇਹ ਸਾੜੀ ਕਿੰਨੇ ਦੀ ਹੈ", "punjabi"),
    ("ଏହି ଶାଢ଼ୀର ଦାମ କେତେ", "odia"),
    ("یہ ساڑھی کتنے کی ہے", "urdu"),
    ("chaniya choli che?", "gujarati_roman"),
    ("aa saree no price shu che", "gujarati_roman"),
    ("tumchyakade saree ahe ka", "marathi_roman"),
    ("mujhe saree chahiye", "hindi_roman"),
    ("do you have red saree", "english"),
])
def test_detect_language_any_script(text, expected):
    """Every supported script / Roman variant is recognised, punctuation included."""
    assert detect_language(text) == expected


@pytest.mark.parametrize("text", ["Rajesh Patel", "Ahmedabad, Navrangpura 380009", "COD", "2", "ok", "hi", "kurti", "😊"])
def test_no_signal_message_keeps_previous_language(text):
    """Names, addresses, digits, 'ok' and bare greetings never flip an established language."""
    assert detect_language(text, previous_language="gujarati_script") == "gujarati_script"
    assert detect(text, "gujarati_script").confident is False


def test_script_beats_previous_language():
    """A clearly written message switches language even when the previous turn differed."""
    assert detect_language("ભાવ શું છે?", previous_language="english") == "gujarati_script"
    assert detect_language("what is the price", previous_language="gujarati_script") == "english"


def test_normalize_language_and_expected_script():
    """Legacy spellings fold into known codes and scripts are looked up per language."""
    assert normalize_language("hinglish") == "hindi_roman"
    assert normalize_language("gu") == "gujarati_script"
    assert normalize_language("klingon") == "english"
    assert expected_script("gujarati_script") == "gujarati"
    assert expected_script("hindi_roman") is None


def test_script_counts_ignores_digits_and_emoji():
    """Only letters count toward a script; matras count with their script."""
    counts = script_counts("ચણિયા 123 😊 saree")
    assert counts["gujarati"] >= 4 and counts["latin"] == 5


def test_is_ambiguous_flags_short_replies():
    """'yes' / 'COD' / digits carry no language signal."""
    assert is_ambiguous("COD") and is_ambiguous(" 2 ") and not is_ambiguous("kem cho")


def test_roman_language_mismatch_flags_wrong_language_latin_replies():
    """An English reply to a Hinglish customer (and vice versa) is flagged; short product lines never are."""
    from app.services.language_service import roman_language_mismatch as mismatch

    assert mismatch("Our team will assist you shortly. Please reply yes to use this address", "hindi_roman")
    assert mismatch("Haan ji, hamare paas yeh available hai. Kaunsa color chahiye aapko?", "english")
    assert not mismatch("Haan ji, hamare paas yeh available hai. Kaunsa color chahiye aapko?", "hindi_roman")
    assert not mismatch("Yes, we have this available in blue and pink. Which colour would you like?", "english")
    assert not mismatch("Printed Kurti [KU30001] — ₹450 Cotton Kurta", "hindi_roman")
    assert not mismatch("Our team will assist you shortly. Please reply yes", "gujarati_script")   # script languages: other check
