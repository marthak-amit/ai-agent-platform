"""Per-turn invariants for the conversation simulator."""

from __future__ import annotations

import re

from app.services import language_service as ls
from tests.sim.harness import CATALOGUE

# Latin words that may legitimately stay in a native-script reply: brands/payment words, order-number
# prefix, the shop name, and the (English) catalogue — product names, categories, colours, sizes.
_ALLOWED_LATIN = {
    "upi", "cod", "gpay", "phonepe", "paytm", "whatsapp", "qr", "sku", "ord", "id", "ok", "pdf", "http", "https",
    "www", "com", "sim", "router", "legacy", "store", "shop", "xxl", "xl", "xs", "nan", "hi", "bhim", "pay",
}
for _sku, _name, _price, _cat, _stock, _variants in CATALOGUE:
    _ALLOWED_LATIN.update(w.lower() for w in re.findall(r"[A-Za-z]+", _name))
    _ALLOWED_LATIN.update(w.lower() for w in re.findall(r"[A-Za-z]+", _cat))
    _ALLOWED_LATIN.add(_sku.lower())
    for _color, _size, *_ in _variants:
        _ALLOWED_LATIN.add(_color.lower())
        if _size:
            _ALLOWED_LATIN.add(_size.lower())

_PLACEHOLDER = re.compile(r"\{[a-z_0-9]+\}|₹\s*\[|\{\{|__AI_REPLY__|\bNone\b|\bnull\b|\bundefined\b|Traceback|NaN|%\(|<built-in")
_AMOUNT = re.compile(r"₹\s?([\d,]+(?:\.\d+)?)")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_URL = re.compile(r"https?://\S+|\S+@\S+")


def _allowed_amounts() -> set[int]:
    """Every price a reply may show: product/variant prices × quantities 1..60, plus small constants."""
    prices = {int(p) for _s, _n, p, *_ in CATALOGUE}
    out = {0}
    for price in prices:
        out.update(price * q for q in range(1, 61))
    return out


_AMOUNTS = _allowed_amounts()


def impure_words(text: str) -> list[str]:
    """Latin words in `text` that are not brands/codes/catalogue words (they should not be in a native-script reply)."""
    text = _URL.sub(" ", text)
    out = []
    for w in _LATIN_WORD.findall(text):
        lw = w.lower()
        if lw in _ALLOWED_LATIN or re.fullmatch(r"[a-z]{2,4}\d+", lw):
            continue
        out.append(w)
    return out


def _in_catalogue(word: str) -> bool:
    """Catalogue word, or the cut-off start of one (WhatsApp truncates button titles at 20 chars)."""
    word = word.lower()
    return word in _ALLOWED_LATIN or any(a.startswith(word) for a in _ALLOWED_LATIN if len(word) >= 2)


def check_turn(session, rec, lang: str | None) -> None:
    """Run every per-turn invariant against the bot's replies to one customer message (lang None = nothing to expect yet)."""
    for sent in rec.replies:
        text = sent.text or ""
        # 1. placeholders / debug leaks
        m = _PLACEHOLDER.search(text)
        if m:
            session.flag("leak", f"placeholder/debug text {m.group(0)!r}", rec)
        # 2. WhatsApp limits
        if len(text) > 4096:
            session.flag("too_long", f"{len(text)} chars", rec)
        if len(sent.buttons) > 3:
            session.flag("too_many_buttons", str(len(sent.buttons)), rec)
        for _bid, title in sent.buttons:
            if title and len(title) > 20:
                session.flag("button_title_len", f"{title!r} ({len(title)})", rec)
        if len(sent.rows) > 10:
            session.flag("too_many_rows", str(len(sent.rows)), rec)
        for _rid, title, _d in sent.rows:
            if title and len(title) > 24:
                session.flag("row_title_len", f"{title!r} ({len(title)})", rec)
        # 3. amounts must be real prices / quantity multiples
        for amount in _AMOUNT.findall(text):
            try:
                value = int(float(amount.replace(",", "")))
            except ValueError:
                continue
            if value not in _AMOUNTS:
                session.flag("unknown_amount", f"₹{amount}", rec)
        # 4. language / script
        if lang is None or lang == "any":
            continue
        if text.strip() and not ls.reply_matches_language(text, lang, frozenset(_ALLOWED_LATIN)):
            session.flag("wrong_language", f"expected {lang}; reply is in the wrong script", rec)
        elif ls.expected_script(lang) is not None and text.strip():
            bad = impure_words(text)
            if bad:
                session.flag("impure_language", f"expected pure {lang}; Latin words: {sorted(set(bad))[:8]}", rec)
        for bid, title in sent.buttons:
            # Product / colour / size buttons carry the catalogue's own spelling on purpose.
            if (title and ls.expected_script(lang) is not None and not ls.reply_matches_language(title, lang, frozenset(_ALLOWED_LATIN)) and len(title) > 3
                    and not all(_in_catalogue(w) for w in re.findall(r"[A-Za-z]+", title))):
                session.flag("button_language", f"button {title!r} not in {lang}", rec)
