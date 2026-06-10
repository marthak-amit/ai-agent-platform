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
    """Non-ASCII message with no matching keywords → hinglish default.

    "₹500 main chahun" uses ₹ (non-ASCII) so the English path is skipped,
    and neither the Gujarati nor Hindi keyword lists match.
    """
    assert detect_language("₹500 main chahun") == "hinglish"


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
    ("gujarati_roman",    "Gujarati-English mix"),
    ("hinglish",          "Hinglish"),
])
def test_get_language_instruction_contains_key_phrase(lang: str, expected_fragment: str):
    """Each language code must produce an instruction mentioning the key phrase."""
    instruction = get_language_instruction(lang)
    assert expected_fragment in instruction, (
        f"Expected '{expected_fragment}' in instruction for lang='{lang}'"
    )


def test_get_language_instruction_unknown_lang_returns_hinglish():
    """An unknown language code falls through to the Hinglish default."""
    result = get_language_instruction("klingon")
    assert "Hinglish" in result


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
