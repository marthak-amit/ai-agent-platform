"""
Agent v3 — small pure text helpers shared by the executor, the invariants and the fast paths (no DB, no LLM).

* ``sanitize_question`` — the only way planner-written words can reach a customer (no prices, dates, stock claims …).
* ``numbers_in`` — digits and number words in English / Hinglish / Gujarati / Hindi, for "did the customer say this quantity?".
* ``value_in_text`` — "did the customer's message produce this colour / size / name / payment?" (aliases included).
* ``looks_like_confirmation`` — an explicit yes to a summary.
"""

from __future__ import annotations

import re

from app.services import command_words as cw
from app.services import indic_vocab

_UNSAFE_RE = re.compile(
    r"[₹$]|\brs\.?\s*\d|\brupees?\b|\d|\b(?:price|cost|discount|offer|stock|delivery|deliver|arrive|days?|ship(?:ped|ping)?|"
    r"paid|payment\s+(?:received|confirmed)|refund|warranty|guarantee|order\s+(?:is|has|was)|confirmed|http|www)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def sanitize_question(text: str | None, *, max_chars: int = 180) -> str | None:
    """
    The planner's free-text question, or None when it states anything the engine owns.

    Rejected: digits, currency, URLs and any price / stock / delivery / payment / order-status wording. Kept to two short
    sentences. The caller then falls back to a templated question built from real data.
    """
    text = " ".join((text or "").split())
    if not text or _UNSAFE_RE.search(text):
        return None
    text = " ".join(_SENTENCE_SPLIT_RE.split(text)[:2]).strip()
    return text[:max_chars].rstrip() or None


# ── numbers ──────────────────────────────────────────────────────────────────

_NUMBER_WORDS: dict[str, int] = {
    **{w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())},
    "dozen": 12, "couple": 2, "pair": 2, "single": 1, "a": 1, "an": 1,
    # Hinglish / Roman Gujarati
    "ek": 1, "ak": 1, "do": 2, "doo": 2, "be": 2, "teen": 3, "tin": 3, "tran": 3, "char": 4, "chaar": 4,
    "paanch": 5, "panch": 5, "paach": 5, "chhe": 6, "che": 6, "cheh": 6, "chah": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8,
    "nau": 9, "nav": 9, "das": 10, "dus": 10,
    # Devanagari / Gujarati script
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    "એક": 1, "બે": 2, "ત્રણ": 3, "ચાર": 4, "પાંચ": 5, "છ": 6, "સાત": 7, "આઠ": 8, "નવ": 9, "દસ": 10,
}
_MORE_WORDS = frozenset({"more", "another", "aur", "extra", "additional", "vadhu", "vadhare", "ek aur", "bija", "biji", "dusra"})


def numbers_in(text: str) -> set[int]:
    """Every quantity the message could be stating: digits (any script) and number words; "one more"-style adds 1."""
    nums: set[int] = set()
    tokens = indic_vocab.tokenize(text)
    for tok in tokens:
        if tok.isdecimal():
            try:
                nums.add(int(tok))
            except ValueError:
                pass
        elif tok in _NUMBER_WORDS:
            nums.add(_NUMBER_WORDS[tok])
    if any(t in _MORE_WORDS for t in tokens):
        nums.add(1)
    return nums


def is_bare_number(text: str) -> int | None:
    """The integer when the whole message is just a number ("2", "૩", "two"), else None."""
    tokens = indic_vocab.tokenize(text)
    if len(tokens) != 1:
        return None
    tok = tokens[0]
    if tok.isdecimal():
        try:
            return int(tok)
        except ValueError:
            return None
    return _NUMBER_WORDS.get(tok) if tok not in ("a", "an", "do", "che", "be") else None


# Words that are a number ONLY as the answer to "how many?" ("do kurti" is not a bare number elsewhere: "do" is also the English verb).
_QUANTITY_ANSWER_WORDS: dict[str, int] = {"be": 2, "do": 2, "doo": 2, "ek": 1, "ak": 1}


def quantity_answer(text: str) -> int | None:
    """
    The quantity when `text` answers "how many?": a bare number in any script / language ("2", "૨", "२", "two", "બે", "दो") or the
    Roman number words that double as other words elsewhere ("be" = 2 in Gujarati, "do" = 2 in Hindi).
    """
    n = is_bare_number(text)
    if n is not None:
        return n
    tokens = indic_vocab.tokenize(text)
    return _QUANTITY_ANSWER_WORDS.get(tokens[0]) if len(tokens) == 1 else None


# ── grounding ────────────────────────────────────────────────────────────────

_PAYMENT_WORDS: dict[str, frozenset[str]] = {
    "COD": frozenset({"cod", "cash", "cashondelivery", "nakad", "nakd", "नकद", "कैश", "કેશ", "રોકડ", "paisa", "delivery"}),
    "UPI": frozenset({"upi", "gpay", "googlepay", "phonepe", "paytm", "bhim", "online", "qr", "scan", "યુપીઆઈ", "यूपीआई"}),
}


def _norm_value(value: str | None) -> str:
    """Lower-case, single-spaced."""
    return " ".join(indic_vocab.tokenize(value or ""))


def value_in_text(attr: str, value: str | None, text: str) -> bool:
    """
    Did the customer's `text` say this `value` for `attr` ('color' / 'size' / 'material' / 'payment' / 'name' / 'address' / 'mobile')?

    Lenient on purpose — it is a guard against the planner inventing a value, not a parser: exact word(s), the shared colour
    vocabulary ("laal" ≙ Red, "ગુલાબી" ≙ Pink), a one-letter-off spelling, or (name / address) every word of the value.
    """
    if value in (None, ""):
        return True
    tokens = indic_vocab.tokenize(text)
    token_set = set(tokens)
    want = _norm_value(str(value))
    if not want:
        return True
    if attr == "payment":
        words = _PAYMENT_WORDS.get(str(value).upper(), frozenset())
        joined = "".join(tokens)
        return bool(token_set & words) or any(w in joined for w in ("cashondelivery", "payondelivery"))
    if attr == "mobile":
        digits = re.sub(r"\D", "", text)
        return re.sub(r"\D", "", str(value))[-10:] in digits
    if attr in ("name", "address"):
        return all(w in token_set or any(_close(w, t) for t in tokens) for w in want.split())
    # color / size / material
    padded = f" {' '.join(tokens)} "
    if f" {want} " in padded:
        return True
    if attr == "color":
        mine = indic_vocab.canonical_colors(str(value)) or [want]
        said = indic_vocab.canonical_colors(text)
        if set(mine) & set(said) or (len(want.split()) > 1 and any(w in said for w in mine)):
            return True
        # "dark green" should also be reached by "green"
        if any(part in token_set for part in want.split() if len(part) > 2):
            return True
    return any(_close(want, t) for t in tokens if len(t) > 2)


def _close(a: str, b: str) -> bool:
    """True when two words are the same up to one typo (only for words of 4+ letters)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 1:
        return False
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


_SLOT_WORDS: dict[str, frozenset[str]] = {
    "color": frozenset({"color", "colour", "colors", "colours", "rang", "rango", "rangna", "shade", "shades", "रंग", "रंगों", "રંગ", "રંગો"}),
    "size": frozenset({"size", "sizes", "naap", "nap", "साइज", "साइज़", "સાઇઝ", "સાઈઝ", "નાપ"}),
    "material": frozenset({"material", "fabric", "kapda", "kapdu", "कपड़ा", "कपडा", "કાપડ"}),
    "quantity": frozenset({"qty", "quantity", "pieces", "piece", "pcs", "nos", "kitne", "ketla", "ketli", "कितने", "કેટલા", "कितना"}),
}


def mentions_slot(text: str, slot: str) -> bool:
    """Does the message talk about this slot at all ("change the colour", "size kya hai", "ketla pieces")? Lenient on purpose."""
    return bool(set(indic_vocab.tokenize(text)) & _SLOT_WORDS.get(slot, frozenset()))


# ── confirmation ─────────────────────────────────────────────────────────────

_CONFIRM_PHRASES = (
    "confirm", "confirmed", "place order", "place the order", "place it", "go ahead", "book it", "book my order",
    "order kar do", "order karo", "kar do", "kardo", "confirm kar", "pakka", "done deal", "yes please", "sure", "ok confirm",
    "chalo", "theek hai", "thik che", "barabar", "kari do", "karo",
)
_NEGATION = frozenset({
    "no", "not", "dont", "don't", "cancel", "change", "wait", "but", "nahi", "nahin", "na", "mat", "nathi", "nai",
    "stop", "hold", "later", "different", "wrong", "galat",
})


def looks_like_confirmation(text: str) -> bool:
    """
    An explicit "yes, place it": a yes word (any of 15 languages) or a confirm phrase — and no negation, change or question
    in the same message ("yes but change the address" is NOT a confirmation).
    """
    if not (text or "").strip() or "?" in text or "؟" in text:
        return False
    norm = cw.normalize(text)
    tokens = set(norm.split())
    if tokens & _NEGATION or cw.matches("no", text):
        return False
    if cw.is_yes(text):
        return True
    padded = f" {norm} "
    return any(f" {p} " in padded for p in _CONFIRM_PHRASES) and len(tokens) <= 8
