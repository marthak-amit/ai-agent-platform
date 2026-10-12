"""Multilingual yes / no / cancel / change vocabulary used by the keyword (non-LLM) order flow."""

import pytest

from app.services import command_words as cw


@pytest.mark.parametrize("text", ["yes", "Haan ji", "हाँ", "हां जी", "હા", "હા જી", "ਹਾਂ", "ஆம்", "అవును", "جی ہاں", "hoy", "ok"])
def test_yes_in_many_languages(text):
    """Short affirmative replies are recognised in every supported language."""
    assert cw.is_yes(text), text


@pytest.mark.parametrize("text", ["no", "nahi", "नहीं", "ના", "இல்லை", "نہیں", "nahi, ok", "રદ કરો"])
def test_yes_is_false_for_no_and_cancel(text):
    """'no' words and cancel phrases are never a yes, even next to an 'ok'."""
    assert not cw.is_yes(text), text


@pytest.mark.parametrize("text", [
    "cancel", "Cancel my order", "ऑर्डर कैंसल कर दो", "ऑर्डर रद्द करें", "મારો ઓર્ડર રદ કરો", "મારે હવે નથી લેવું", "ஆர்டரை ரத்து செய்யுங்கள்",
    "آرڈر منسوخ کریں", "order cancel kara", "ਆਰਡਰ ਰੱਦ ਕਰੋ",
])
def test_cancel_in_many_languages(text):
    """Cancel statements are recognised whatever the language or script."""
    assert cw.is_cancel(text), text


@pytest.mark.parametrize("text", ["when can I cancel?", "cancel kab tak kar sakte hain?", "I cannot stop thinking about this", "નાની સાડી જોઈએ"])
def test_cancel_not_triggered_by_questions_or_lookalikes(text):
    """A question about cancelling, or a word that merely contains a cancel/no syllable, is not a cancel."""
    assert not cw.is_cancel(text), text


def test_normalize_keeps_indic_vowel_signs():
    """Combining marks (matras, viramas) must survive normalisation — a regex \\w would delete them."""
    assert cw.normalize("નાની સાડી!") == "નાની સાડી"
    assert cw.normalize("अलग-अलग 🙏") == "अलग अलग"


def test_single_words_match_whole_words_only():
    """'ના' (no) must not fire inside 'નાની' (small)."""
    assert cw.matches("no", "ના")
    assert not cw.matches("no", "નાની સાડી")


def test_payment_option_words_in_native_scripts():
    """COD / UPI answers in Hindi and Gujarati script."""
    assert cw.matches_whole("pay_cod", "કેશ ઓન ડિલિવરી")
    assert cw.matches_whole("pay_cod", "कैश ऑन डिलीवरी")
    assert cw.matches_whole("pay_upi", "ઓનલાઇન")
    assert not cw.matches_whole("pay_cod", "સાડી")


def test_smalltalk_only_is_whole_message():
    """A greeting alone is smalltalk; a greeting followed by a product question is not."""
    assert cw.is_smalltalk_only("કેમ છો?")
    assert cw.is_smalltalk_only("धन्यवाद")
    assert not cw.is_smalltalk_only("hi, kurti available?")


def test_yes_must_be_short():
    """A long sentence that happens to contain 'ji' is not a confirmation."""
    assert not cw.is_yes("kitne ka hai ji ye wala mujhe batao please")
