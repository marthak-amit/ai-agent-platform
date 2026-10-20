"""
Agent v3 — a message made only of the shop's own words (product nouns, categories, colours, fabrics) plus chatter is a catalogue lookup:
no planner call.

"Saree" / "suit" / "kurti" (any spelling the shop's vocabulary corrects) → browse that category.
"yellow saree" · "Chudidar suit georgette suite" · "dikha bhai red kurti" · "saree dekh" → search for exactly those words.

Everything else — a digit (price, quantity), a negation (remove / cancel / "nahi"), a word the shop does not sell and that is not chatter —
returns None and the planner decides. Hindi-roman chatter ("dekh", "dikha", "deke", "bhai", "de do", "wala", "chahiye" …) is skipped, never
fuzzy-matched. Tenant-scoped: only THIS catalogue's words count (``Catalogue.fuzzy_vocabulary``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services import agent_query, indic_vocab
from app.services import command_words as cw
from app.services.agent_catalog import Catalogue

_FILLER = frozenset("""
show me give tell send share see look looking please pls plz i we you the a an some any all of in for and or with to is are
available have has latest new options option designs design collection variety models model types type more other another kuch koi
dikhao dikha dikhado dikhade dikhaiye dikhana dekh dekho dekhna dekhiye dekhe deke dekhao de dede dedo dena bhai bhaiya didi ji sir madam
mujhe mujhko mereko mereku mera meri mere mare mane muje wala wali wale vala vali ka ki ke ko liye mein me hai hain he che chhe batao
bataiye bata bhejo bhej aur ya se par sab saare saari sabhi kaun kaunsi kaunse kya hi to na
""".split())
# Words that only say "I'd like …": "I want kurti", "need saree", "kurti chahiye", "saree joiye" are still a plain lookup of the noun, so
# they are dropped before the message is read (a digit, a size or a negation still sends it to the planner).
_INTENT_STRIP = frozenset("""
want wants wanted need needs buy purchase take get chahiye chahie chaiye chahta chahti chahta-hun joiye joie joi lena lunga lungi lelo
le khareedna kharidna levi levu leva
""".split())
# Words that put something INTO the order or name a quantity / size: the message is an order (planner → add_line), never a plain lookup.
_ORDER_INTENT = frozenset("order add book size sizes qty quantity piece pieces pcs pc".split())
_SIZE_WORDS = frozenset("xs s m l xl xxl xxxl xxxxl 2xl 3xl 4xl free onesize".split())
_NEGATIONS = frozenset("no not nahi nahin nhi nathi nako mat dont don't cancel remove delete hata hatao hatado kadhi nakho without except".split())
_PHRASE_PAIRS = re.compile(r"\b(?:de|dede|deke|dikha|dikhao|dekh|bata|batao|send|show|bhej|bhejo)\s+(?:do|lo)\b", re.IGNORECASE)
_MAX_TOKENS = 8


@dataclass(frozen=True)
class CatalogueLookup:
    """What a catalogue-words message asks for: browse a category, or search for these words."""

    kind: str            # "category" | "product" (value = sku) | "search"
    value: str


def _in_name(word: str, view) -> bool:
    """Is `word` (or its singular) a word of the product's name?"""
    words = {agent_query._singular(w) for w in indic_vocab.tokenize(view.name)}
    return agent_query._singular(word) in words


def parse(text: str, catalogue: Catalogue) -> CatalogueLookup | None:
    """The lookup `text` asks for, or None when it is not made only of the shop's words and chatter (the planner then decides)."""
    cleaned = _PHRASE_PAIRS.sub(" ", cw.strip_leading_greeting(text or ""))
    raw_tokens = indic_vocab.tokenize(cleaned)
    tokens = [t for t in raw_tokens if t not in _INTENT_STRIP]
    had_intent = len(tokens) != len(raw_tokens)
    lookup = _lookup(tokens, catalogue)
    # "I want kurti" / "kurti chahiye" is a category browse — but "I want a leather wallet" / "I want a red kurti" names ONE product / a
    # specific thing to buy: that stays with the planner (add_line), exactly as before.
    return lookup if lookup is not None and (not had_intent or lookup.kind == "category") else None


def _lookup(tokens: list[str], catalogue: Catalogue) -> CatalogueLookup | None:
    """The lookup the (intent-stripped) `tokens` ask for, or None."""
    if not tokens or len(tokens) > _MAX_TOKENS:
        return None
    if any(t in _NEGATIONS or t in _ORDER_INTENT or t in _SIZE_WORDS or any(ch.isdigit() for ch in t) for t in tokens):
        return None
    vocab = catalogue.fuzzy_vocabulary()
    match = catalogue.normalize_query(" ".join(t for t in tokens if t not in _FILLER))
    kept = indic_vocab.tokenize(match.text)
    if not kept:
        return None
    product_words: list[str] = []
    attr_words: list[str] = []
    for tok in kept:
        single = agent_query._singular(tok)
        if tok in vocab.words or single in vocab.words:
            product_words.append(tok)
        elif tok in vocab.attr_values or single in vocab.attr_values:
            attr_words.append(tok)
        else:
            return None                      # a word the shop does not use and that is not chatter: not a plain catalogue lookup
    if not product_words:
        return None                          # only colours / sizes ("red"): that is an answer to something — the planner reads it
    if len(set(attr_words)) > 1:
        return None                          # "red kurti in pink": two colours / values — which one is meant is the planner's call
    if not attr_words:
        named = [v for v in catalogue.views if all(_in_name(w, v) for w in product_words)]
        if len(named) == 1 and len(product_words) >= 2:
            return CatalogueLookup("product", named[0].sku)          # "show me the tote bag": exactly one product carries all these words
    if not attr_words and match.categories:
        category_words = {agent_query._singular(w) for c in match.categories for w in indic_vocab.tokenize(c)}
        if all(agent_query._singular(t) in category_words for t in product_words):
            return CatalogueLookup("category", " ".join(product_words))
    return CatalogueLookup("search", " ".join(kept))
