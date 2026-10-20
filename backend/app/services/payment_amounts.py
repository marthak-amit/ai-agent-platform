"""
Rupee amounts read from the OCR text of a payment screenshot (GPay, PhonePe, Paytm, BHIM, bank SMS / net-banking screens).

conv=60 (ORD-2026-0077): a screenshot of ₹8,548 was read as ``[8.0, 7.0]`` — OCR puts spaces inside the number ("₹ 8 548"), splits it
across tokens ("8, 548"), and the old regex also happily read phone numbers, dates, times and UTR digits as amounts.

``read_amounts`` therefore works in two steps:

1. **Blank out what is not money** — dates, times, phone numbers, percentages, UPI ids, labelled references (UTR / Ref No / Txn id /
   order id), masked account numbers and any digit run too long to be a price (≥ 9 digits without separators).
2. **Collect candidates** — *strong*: a number right after a currency marker (₹ · Rs · Rs. · INR · Rupees, and the glyphs Tesseract
   typically makes of ₹: ``%`` ``€`` ``T`` ``Z`` ``?``) or followed by ``/-``; *weak*: any other grouped (1,299) or decimal (1299.00) number.
   Indian (1,50,000) and international (150,000) grouping and OCR-spaced groups ("8 548", "8, 548", "8,548 .00") are all understood.

``extract_amounts`` returns the strong amounts when there are any (a receipt states its amount next to ₹), else the weak ones — so a
stray number on the screen cannot be mistaken for the order total when the receipt clearly shows another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_AMOUNT = 10_000_000.0          # ₹1 crore: nothing a boutique sells costs more
MIN_AMOUNT = 10.0                  # below this a "number" on a screenshot is a digit of noise, a list index or a count — never a payment

# ── step 1: what is not money ────────────────────────────────────────────────

_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NOISE_PATTERNS = [
    # dates: 12/10/2026 · 12-10-26 · 2026-10-12 · 12 Oct 2026 · Oct 12, 2026 · 12th Oct
    re.compile(r"\b\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4}\b"),
    re.compile(r"\b\d{4}\s*[/\-]\s*\d{1,2}\s*[/\-]\s*\d{1,2}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s,\-]+{_MONTH}\.?(?:[\s,\-]+\d{{2,4}})?\b", re.IGNORECASE),
    re.compile(rf"\b{_MONTH}\.?[\s,\-]+\d{{1,2}}(?:st|nd|rd|th)?(?:[\s,\-]+\d{{2,4}})?\b", re.IGNORECASE),
    # times: 10:45 · 10:45:12 · 10:45 AM · 10.45 pm
    re.compile(r"\b\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?(?:\s*[ap]\.?m\.?)?", re.IGNORECASE),
    re.compile(r"\b\d{1,2}\.\d{2}\s*[ap]\.?m\.?\b", re.IGNORECASE),
    # percentages
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),
    # UPI ids (name@bank) and e-mail-like handles
    re.compile(r"[A-Za-z0-9._\-]{2,}@[A-Za-z][A-Za-z0-9.]{1,}"),
    # references: "UTR 412345678901" · "UPI Ref No: 4123…" · "Txn ID T2610…" · "Order ID OD1234…" · "Transaction ID …"
    re.compile(
        r"\b(?:utr|rrn|upi\s*ref(?:erence)?(?:\s*(?:no|number|id))?|ref(?:erence)?\s*(?:no|number|id|#)?|txn\s*(?:id|no|number|ref)?|"
        r"transaction\s*(?:id|no|number|ref)?|order\s*id|payment\s*id|receipt\s*(?:no|number|id))\b[\s:#.\-]*[A-Za-z0-9]{6,}", re.IGNORECASE,
    ),
    # our own order numbers: ORD-2026-0077
    re.compile(r"\bord[\s\-]*\d{4}[\s\-]*\d{1,6}\b", re.IGNORECASE),
    # masked account / card numbers: XXXX1234 · A/c ending 1234 · a/c no. XX1234 · ****1234
    re.compile(r"(?:\b[xX*]{2,}\s*\d{2,6}\b|[*•]{2,}\s*\d{2,6}\b)"),
    re.compile(r"\b(?:a/?c|account|acct|card)\b[^\d\n]{0,12}(?:ending|no\.?|number|#)?[^\d\n]{0,4}\d{3,18}\b", re.IGNORECASE),
    # phone numbers: +91 98765 43210 · 98765-43210 · 9876543210 · 0261 2345678
    re.compile(r"(?<![\d,.])(?:\+\s*91[\s\-]*|91[\s\-]+|0)?[6-9]\d{4}[\s\-]?\d{5}(?![\d,])"),
    re.compile(r"(?<![\d,.])(?:\+\s*91[\s\-]*)?\(?0\d{2,4}\)?[\s\-]?\d{6,8}(?![\d,])"),
]
_LONG_RUN_RE = re.compile(r"(?<![\d,.])\d{9,}(?![\d,])")        # an unseparated run this long is a reference, not a price

# ── step 2: candidates ───────────────────────────────────────────────────────

_GROUPED = r"\d{1,3}(?:\s*,\s*\d{2,3})+"                       # 8,548 · 1,50,000 · 8, 548 (OCR gap after the comma)
_SPACED = r"\d{1,3}(?:\s\d{3})+"                                # 8 548 · 1 50 000 is not read (ambiguous) — OCR spacing of thousands
_PLAIN = r"\d+"
_NUMBER = rf"(?:{_GROUPED}|{_SPACED}(?![\d])|{_PLAIN})(?:\s?\.\s?\d{{1,2}})?"
_CUR = r"(?:₹|\brs\b\.?|\binr\b|\brupees?\b|[%€¥?]|(?<![A-Za-z])[TZz](?=\s?\d{1,3}\s?[,\s]\s?\d{3}))"
_STRONG_RE = re.compile(rf"{_CUR}\s*[:\-]?\s*({_NUMBER})(?!\d)", re.IGNORECASE)
_SLASH_DASH_RE = re.compile(rf"(?<![\d.,])({_NUMBER})\s*/-")
_WEAK_RE = re.compile(rf"(?<![\d.,])((?:{_GROUPED}|{_PLAIN})(?:\s?\.\s?\d{{1,2}})?)(?![\d,])")


@dataclass
class AmountReading:
    """Amounts found in a text: next to a currency marker (``strong``) and elsewhere (``weak``), each largest first."""

    strong: list[float] = field(default_factory=list)
    weak: list[float] = field(default_factory=list)

    @property
    def best(self) -> list[float]:
        """The amounts to match an order against: the strong ones when any, else the weak ones."""
        return self.strong or self.weak


def _to_float(raw: str) -> float | None:
    """'1,50,000' → 150000.0 · '8 548' → 8548.0 · '17,245.00' → 17245.0 · '8,548 .00' → 8548.0 (None when not a plausible amount)."""
    s = re.sub(r"\s+", "", raw)
    if not s:
        return None
    s = s.replace(",", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if MIN_AMOUNT <= value <= MAX_AMOUNT else None


def _blank(text: str) -> str:
    """The text with everything that is not money replaced by spaces (positions stay, so adjacent tokens do not fuse)."""
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return _LONG_RUN_RE.sub(lambda m: " " * len(m.group(0)), text)


_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def looks_like_text(text: str | None) -> bool:
    """
    True when OCR output reads like a screen of words (at least two words of 3+ letters). Tesseract run over a black frame, a blurry photo or a
    texture returns a few hundred characters of ``= 7 | ae -`` — not a receipt — and the digits in that noise must never be taken for an amount.
    """
    return len(_WORD_RE.findall(text or "")) >= 2


def read_amounts(text: str | None) -> AmountReading:
    """
    Strong and weak rupee amounts in an OCR text (see the module docstring).

    Quality gate: a text that does not read like words yields no WEAK amounts (an amount next to ₹ / Rs / INR is still taken — that is a claim
    the text itself makes); amounts under ``MIN_AMOUNT`` are never read.
    """
    cleaned = _blank((text or "").replace(" ", " "))
    strong: set[float] = set()
    for pattern in (_STRONG_RE, _SLASH_DASH_RE):
        for m in pattern.finditer(cleaned):
            value = _to_float(m.group(1))
            if value is not None:
                strong.add(value)
    weak: set[float] = set()
    for m in (_WEAK_RE.finditer(cleaned) if looks_like_text(text) else ()):
        value = _to_float(m.group(1))
        if value is not None and value not in strong:
            weak.add(value)
    return AmountReading(sorted(strong, reverse=True), sorted(weak, reverse=True))


def extract_amounts(text: str | None) -> list[float]:
    """The amounts of a receipt text to compare with order totals (largest first)."""
    return read_amounts(text).best
