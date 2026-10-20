"""
Agent v3 — "2 purple and one red and one green": a ₹0, deterministic reading of a quantity-per-option list for the product in focus.

The planner used to get this wrong in the worst way (conv=60, ORD-2026-0075): one unavailable colour made it discard the valid ones,
it asked a generic question with an example built from the shop's first two colours, and the customer — who copied the example —
ordered a colour they never wanted. This module reads the message itself:

* segments are split on ``, ; & + / |``, newlines and the joiner words of the supported languages ("and", "aur", "ane", "અને", "और" …);
* each segment is ``<qty> <option>`` or ``<option> <qty>`` — a quantity is digits (Latin, Gujarati, Devanagari) or a number word
  (one/two, ek/do/teen, ek/be/tran, એક/બે, एक/दो …), an option is matched against THIS product's variant values (colour, size, material;
  the shared colour vocabulary and one-letter typos included);
* an option that is a recognisable colour/size word but not sold for this product ("green") is reported as unavailable — it never
  cancels the valid pairs.

``parse`` returns None whenever any segment is not of that shape (a name, a question, another product …): the planner decides then.
Nothing here touches the database, the cart or an LLM; ``agent_fast_paths`` turns the result into ``add_line`` actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import indic_vocab
from app.services.agent_catalog import SIZE_LADDER, ProductView
from app.services.agent_text import _NUMBER_WORDS

# Words that join two segments (a token on its own) — Latin, Gujarati, Devanagari.
_JOINERS = frozenset({"and", "aur", "ane", "ani", "aani", "tatha", "plus", "n", "અને", "તથા", "और", "तथा", "आणि", "व", "ओर"})
_SEPARATOR_RE = re.compile(r"[,;&+/|\n\r]+")
# Words around a pair that carry no option and no quantity.
_FILLER = frozenset("""
pieces piece pcs pc nos no of x wala wali vala vali ka ki ke ma mein in colour color colours colors rang size please pls plz the
i want need give me mujhe mujhko mane amne joiye joie chahiye chahie dena dedo de karo kar sir ji bhai
ohh oh ohhh ohk okay ok hmm hm um uh actually basically so also just only kindly make change update it to
પીસ નંગ રંગ જોઈએ મને
पीस रंग चाहिए मुझे कृपया
""".split())
# Number words that are ordinary words in some language of ours and never a quantity inside this parser.
_NOT_QUANTITY = frozenset({"a", "an", "che", "chhe", "cheh", "chah", "single", "couple", "pair", "dozen", "no", "n"})
_QTY_X_RE = re.compile(r"^(?:x(\d{1,4})|(\d{1,4})x)$")
_SIZE_WORDS = frozenset(SIZE_LADDER)


@dataclass
class Pair:
    """One resolved ``<qty> <option>``: the catalogue spelling of the option and the quantity (None = the customer gave no number)."""

    attr: str
    value: str
    qty: int | None


@dataclass
class Missing:
    """An option the customer asked for that this product is not sold in (a colour/size word, e.g. "green")."""

    attr: str
    text: str
    qty: int | None


@dataclass
class Parse:
    """What a quantity-per-option message asks for."""

    sku: str
    pairs: list[Pair] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)

    @property
    def segments(self) -> int:
        """How many ``<qty> <option>`` segments the message had."""
        return len(self.pairs) + len(self.missing)


def _quantity(token: str) -> int | None:
    """The quantity a single token states (digits in any script, a number word, ``2x`` / ``x2``), else None."""
    if token.isdecimal():
        try:
            n = int(token)
        except ValueError:
            return None
        return n if 1 <= n <= 10_000 else None
    m = _QTY_X_RE.match(token)
    if m:
        return int(m.group(1) or m.group(2))
    if token in _NOT_QUANTITY:
        return None
    n = _NUMBER_WORDS.get(token)
    return n if n is not None and n >= 1 else None


def _split_on_quantities(tokens: list[str]) -> list[list[str]]:
    """
    One run of words without a joiner ("2 purple 1 red", "purple 2 red 1") cut into ``<qty> <option>`` / ``<option> <qty>`` segments.

    Quantity-first when the run starts with a number (a number then begins each new segment), otherwise quantity-last (a number ends
    each segment). A run with at most one number is one segment.
    """
    positions = [i for i, t in enumerate(tokens) if t not in _FILLER and _quantity(t) is not None]
    if len(positions) <= 1:
        return [tokens]
    out: list[list[str]] = []
    if positions[0] == 0:
        bounds = positions + [len(tokens)]
        out = [tokens[a:b] for a, b in zip(bounds, bounds[1:])]
    else:
        start = 0
        for pos in positions:
            out.append(tokens[start:pos + 1])
            start = pos + 1
        if start < len(tokens):
            out.append(tokens[start:])
    return [seg for seg in out if seg]


_GLUED_NUM_FIRST = re.compile(r"^(\d{1,4})([a-z]{2,})$")     # "1gray", "2black"
_GLUED_NUM_LAST = re.compile(r"^([a-z]{3,})(\d{1,4})$")      # "black2"
_SIZE_WITH_DIGIT = re.compile(r"^\d+xl$")                    # "2xl", "3xl": a size, not "2 xl"


def _unglue(tokens: list[str]) -> list[str]:
    """
    Split a quantity glued to its option ("1gray" → "1", "gray"; "black2" → "black", "2"); sizes such as "2xl" / "3xl" stay whole, and so
    do the "2x" / "x2" quantity forms (they are read by ``_QTY_X_RE``).
    """
    out: list[str] = []
    for tok in tokens:
        if _SIZE_WITH_DIGIT.match(tok):
            out.append(tok)
            continue
        m = _GLUED_NUM_FIRST.match(tok) or _GLUED_NUM_LAST.match(tok)
        out.extend(m.groups() if m else [tok])
    return out


def _segments(text: str) -> list[list[str]]:
    """The message cut into token lists, one per ``<qty> <option>`` candidate."""
    out: list[list[str]] = []
    for chunk in _SEPARATOR_RE.split(text or ""):
        current: list[str] = []
        for tok in _unglue(indic_vocab.tokenize(chunk)):
            if tok in _JOINERS:
                if current:
                    out.extend(_split_on_quantities(current))
                current = []
            else:
                current.append(tok)
        if current:
            out.extend(_split_on_quantities(current))
    return out


_SHADE_WORDS = frozenset({"dark", "light", "deep", "bright", "pale", "baby", "sky", "royal", "bottle", "rani", "mehendi"})


def _is_colour_phrase(text: str) -> bool:
    """A colour and nothing else: "green", "dark green", "hara" — NOT "red kurti" (another product) or "pink m" (a colour AND a size)."""
    words = text.lower().split()
    return bool(words) and all(w in _SHADE_WORDS or indic_vocab.canonical_colors(w) for w in words) and any(indic_vocab.canonical_colors(w) for w in words)


def _is_size_like(text: str) -> bool:
    """A size word the shop could sell in ("xl", "m", "xxl") — used to report an unavailable size instead of giving up."""
    return text.lower().replace(" ", "") in _SIZE_WORDS


def _read_segment(tokens: list[str]) -> tuple[int | None, str] | None:
    """
    ``(quantity or None, option text)`` of one segment, or None when it is not of the shape ``<qty> <option>`` / ``<option> <qty>``.

    Filler ("pieces", "wala", "mujhe", "joiye" …) is dropped; the quantity is the first or last remaining token; the rest is the option.
    """
    rest = [t for t in tokens if t not in _FILLER]
    if not rest:
        return None
    qty: int | None = None
    if len(rest) >= 2 and _quantity(rest[0]) is not None:
        qty, rest = _quantity(rest[0]), rest[1:]
    elif len(rest) >= 2 and _quantity(rest[-1]) is not None:
        qty, rest = _quantity(rest[-1]), rest[:-1]
    if not rest or len(rest) > 3 or any(_quantity(t) is not None for t in rest):
        return None
    return qty, " ".join(rest)


def parse(text: str, view: ProductView | None) -> Parse | None:
    """
    Read `text` as a list of ``<qty> <option>`` pairs for the product `view`; None when it is anything else.

    Fires only when at least two segments are understood and at least one of them states a quantity (a bare "red and green" has no
    numbers to apply — the planner asks "how many of each?" with the customer's own options). Duplicates add up ("1 red + 1 red").
    """
    if view is None or not view.has_variants or not view.needed_attrs:
        return None
    segments = _segments(text)
    if len(segments) < 2:
        return None
    result = Parse(sku=view.sku)
    merged: dict[tuple[str, str], Pair] = {}
    for tokens in segments:
        read = _read_segment(tokens)
        if read is None:
            return None
        qty, option = read
        hit: tuple[str, str] | None = None
        for attr in view.needed_attrs:
            values = view.resolve(attr, option)
            if len(values) > 1:
                return None             # ambiguous: let the planner / the customer decide
            if values:
                hit = (attr, values[0])
                break
        if hit is not None:
            key = (hit[0], hit[1].lower())
            if key in merged:
                prev = merged[key]
                prev.qty = (prev.qty or 0) + (qty or 0) if (prev.qty or qty) else None
            else:
                pair = Pair(hit[0], hit[1], qty)
                merged[key] = pair
                result.pairs.append(pair)
            continue
        if _is_colour_phrase(option):
            result.missing.append(Missing("color", option, qty))
        elif _is_size_like(option) and "size" in view.needed_attrs:
            result.missing.append(Missing("size", option.upper(), qty))
        else:
            return None                 # not an option of any kind (a product word, "red kurti", "pink m", a name …): the planner reads it
    if result.segments < 2 or not any(p.qty for p in result.pairs) and not any(m.qty for m in result.missing):
        return None
    return result
