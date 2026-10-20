"""
Agent v3 — "I don't want the kurti": a ₹0 reading of removal sentences, so dropping an item never depends on the planner.

``detect`` returns a ``Removal`` only when the WHOLE message is explained by a removal phrase ("nathi joti", "nahi chahiye",
"mat bhejo", "hatao", "kadhi nakho", "don't want", "remove", "cancel <item>" — English, Hinglish, Roman and native Gujarati /
Hindi / Marathi) plus words that name items in THIS cart (product-name and category words, SKU, colour). Anything else in the
sentence ("…, I want shoes") is left to the planner, which has the same examples in its prompt, so a change of mind is never read
as a plain removal.

A message that names one cart product removes every line of that product (narrowed by a colour/size word when the product has several
lines); a message that names several equally-likely products — or only shared words like "cotton" — asks WHICH one (buttons).
Spelling is tolerated the same way as in search ("kurtee", "sarees"). Nothing here touches the database or an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import command_words as cw
from app.services import indic_vocab
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue

# Removal / "don't want" phrases on normalised text (lower-case, punctuation → space). Left side deliberately has no \b: customers
# glue the noun to the verb ("kurtinathi joti").
_REMOVAL_PATTERNS: tuple[str, ...] = (
    # Gujarati (Roman)
    r"nathi\s*jo(?:i|iti|ti|to|itu|ita|iye|ie|ito|iyu)\b", r"nathi\s*lev(?:i|u|o|a)\b", r"nathi\s*leva\b", r"(?:nai|na)\s+jo(?:i|iti|ti|to|itu|iye|ie)\b",
    r"kadhi\s*nakh(?:o|jo|va|u|yu)\b", r"kadh(?:i|o)\s+do\b", r"hatav(?:o|jo|i\s+do)\b", r"kad(?:h)?i\s*nakho\b",
    # Hindi (Roman)
    r"nahi?n?\s*chahi(?:y|e)e?\b", r"nai\s*chahi(?:y|e)e?\b", r"mat\s*(?:bhejo|bhej|dena|do|dalo|lagao|add\s*karo)\b",
    r"hata\s*(?:do|de|dena|dijiye|diji)\b", r"\bhatao\b", r"\bhatado\b", r"nikal\s*(?:do|de|dena)\b", r"\bnikalo\b",
    r"(?:rehne|rahne)\s*do\b", r"hata(?:iye)?\b",
    # Marathi (Roman)
    r"\bnako\b", r"kadhun\s*taka\b", r"kadh(?:a|u)n\s*tak(?:a|u)\b",
    # English
    r"(?:do\s*n['’]?\s*t|dont|do\s+not)\s+(?:want|need|wanna|like)", r"\bno\s+need\b", r"\bnot\s+(?:needed|required|wanted)\b", r"\bremove\b",
    r"\bdelete\b", r"\bdrop\b", r"take\s+(?:it\s+|this\s+|that\s+)?out\b", r"get\s+rid\s+of\b", r"\bskip\b", r"\bwithout\b",
    r"leave\s+(?:it\s+|this\s+)?out\b", r"\bcancel(?:l)?\b", r"\bcancle\b",
    # Gujarati script
    r"નથી\s*જોઈ(?:તી|તું|તો|તા|એ)", r"નથી\s*જોઇ(?:તી|તું|તો|તા|એ)", r"કાઢી\s*(?:નાખો|નાંખો|નાખ|દો)", r"હટાવ(?:ો|જો)", r"રદ\s*કર(?:ો|જો)?", r"કેન્સલ",
    # Devanagari (Hindi / Marathi)
    r"नहीं\s*चाहिए", r"नही\s*चाहिए", r"नहीं\s*चाहिये", r"हटा\s*(?:दो|दे|दीजिए|दीजिये)", r"हटाओ", r"निकाल\s*(?:दो|दे)", r"मत\s*भेजो",
    r"नको", r"काढून\s*टाका", r"रद्द", r"कैंसल",
)
_REMOVAL_RE = re.compile("|".join(f"(?:{p})" for p in _REMOVAL_PATTERNS))
# Words that may stand around a removal phrase without carrying a request of their own.
_FILLER = frozenset("""
mare mane mujhe mujhko hume humein amne amare ye yeh ya aa a an the this that these those my me i we please pls plz ji bhai bhaiya
didi sir mam madam wala wali vala vali wale ko ne ni nu na no not it its one from of in on cart order orders item items piece pieces
se mathi thi mathi kar do de to also bhi pan and aur ane thee set add just only ek
""".split())


# Words that make a short sentence something other than "remove <noun>": a new request, or a thing that is not an item.
_NOT_AN_ITEM = frozenset("""
want wanna need buy get give show send add order orders instead but also new different another some more other change replace
chahiye chahie joiye joie dikhao dikhana batao bataiye lao dena
payment pay paid address name number mobile phone delivery cart basket everything all sab sabhi badhu badha item items product products
details detail info amount total price discount offer coupon qr upi cod screenshot
""".split())
_ABSENT_MAX_TOKENS = 4


@dataclass
class Removal:
    """What a removal sentence points at."""

    line_ids: list[str] = field(default_factory=list)          # lines to drop now (one product, narrowed by colour when stated)
    ambiguous: list[str] = field(default_factory=list)         # lines of equally likely products → ask which one
    skus: list[str] = field(default_factory=list)
    absent: str | None = None                                   # "chair": a removal of something that is NOT in the cart → say so, change nothing


def cw_negations() -> frozenset[str]:
    """Words that make the "noun" a negation / gate word rather than an item (never reported as "not in your cart")."""
    return frozenset({"yes", "no", "not", "haan", "nahi", "nahin", "ok", "okay", "confirm", "thanks", "thank", "please"})


_LEADING_REMOVAL_RE = re.compile(r"^\s*(?:please\s+|pls\s+)?(?:remove|delete|cancel|hatao|hata\s+do|kadhi\s+nakho|nikal\s+do|nikalo)\b", re.IGNORECASE)


def starts_with_removal_verb(text: str) -> bool:
    """
    True when the message BEGINS with an unmistakable removal verb ("Remove chair", "hatao chair"). Used while a name / address is being
    typed: "Drop Point Surat" is a street, "Remove chair" is not.
    """
    return bool(_LEADING_REMOVAL_RE.match(cw.normalize(text)))


def has_removal_phrase(text: str) -> bool:
    """True when the message contains a removal / "don't want" phrase (any language)."""
    return bool(_REMOVAL_RE.search(cw.normalize(text)))


def _product_words(view) -> set[str]:
    """Lower-case words that name a product: its name and category (3+ letters)."""
    return {w for w in indic_vocab.tokenize(f"{view.name} {view.product.category or ''}") if len(w) >= 3}


def _hits(token: str, words: set[str]) -> set[str]:
    """Product words that `token` is, starts with or ends with (glued forms: "kurtinathi", "tote bagnathi")."""
    out = set()
    for w in words:
        if token == w or (len(w) >= 4 and (token.startswith(w) or token.endswith(w))):
            out.add(w)
    return out


def detect(text: str, cart: CartData, catalogue: Catalogue) -> Removal | None:
    """
    Read `text` as "drop <something in my cart>"; None when it is not that (no removal phrase, nothing in the cart matches, or the
    sentence says anything else as well).
    """
    if not cart.lines or not text or not has_removal_phrase(text):
        return None
    norm = cw.normalize(text)
    remainder = _REMOVAL_RE.sub(" ", norm)
    corrected = catalogue.normalize_query(remainder).text
    tokens = indic_vocab.tokenize(corrected)
    if not tokens:
        return None

    per_sku: dict[str, set[str]] = {}
    for sku in cart.skus:
        view = catalogue.get(sku)
        if view is not None:
            per_sku[sku] = _product_words(view) | {sku.lower()}
    matched: dict[str, set[str]] = {}
    explained: set[str] = set()
    colours = {c for ln in cart.lines for c in indic_vocab.canonical_colors(ln.get("color") or "")}
    sizes = {str(ln["size"]).lower() for ln in cart.lines if ln.get("size")}
    said_colours: set[str] = set()
    for tok in tokens:
        if tok in _FILLER:
            explained.add(tok)
            continue
        if tok in sizes:
            explained.add(tok)
            continue
        canon = set(indic_vocab.canonical_colors(tok))
        if canon & colours:
            said_colours |= canon & colours
            explained.add(tok)
            continue
        hit_any = False
        forms = {tok, *indic_vocab.tokenize(" ".join(indic_vocab.expand_query(tok)))}      # native-script product words → catalogue words
        for sku, words in per_sku.items():
            h = set().union(*(_hits(form, words) for form in forms))
            if h:
                matched.setdefault(sku, set()).update(h)
                hit_any = True
        if hit_any:
            explained.add(tok)
    if not matched:
        # "remove chair": a removal phrase + a noun that names nothing in THIS cart. Say so (deterministically) instead of asking which
        # of the cart's lines to drop. Only for short, plain sentences — "I don't want this, I want shoes" and "cancel payment" are not that.
        loose = [t for t in tokens if t not in explained]
        if (
            loose and len(tokens) <= _ABSENT_MAX_TOKENS and len(loose) <= 2
            and all(t.isalpha() and len(t) >= 3 and t not in _NOT_AN_ITEM and t not in cw_negations() for t in loose)
        ):
            return Removal(absent=" ".join(loose))
        return None
    if any(t not in explained for t in tokens):
        return None

    unique = {sku: words - {w for other, ow in matched.items() if other != sku for w in ow} for sku, words in matched.items()}
    best = max((len(v) for v in unique.values()), default=0)
    if best > 0:
        winners = [sku for sku, v in unique.items() if len(v) == best]
    else:
        winners = list(matched)
    if len(winners) == 1:
        lines = cart.lines_for(winners[0])
        if said_colours and len(lines) > 1:
            narrowed = [ln for ln in lines if set(indic_vocab.canonical_colors(ln.get("color") or "")) & said_colours]
            lines = narrowed or lines
        return Removal(line_ids=[ln["line_id"] for ln in lines], skus=[winners[0]])
    return Removal(ambiguous=[ln["line_id"] for sku in winners for ln in cart.lines_for(sku)], skus=winners)
