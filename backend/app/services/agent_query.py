"""
Agent v3 — catalogue-query normalisation: spelling and transliteration slips become the shop's own words BEFORE a search.

"lahemgha", "lehnga", "lengha" → Lehenga · "suite", "sut" → Suit · "sari", "sadi" → Saree · "chunni" → Dupatta. A word is replaced
only by a word the TENANT's catalogue really uses (product-name and category words), so a typo can never invent a product and one
shop's vocabulary never leaks into another's (the vocabulary is built from that client's catalogue only).

Order per word: already a catalogue word → keep · transliteration alias whose canonical word the shop uses → that word · other
Latin word of 4+ letters → the closest catalogue word with ``rapidfuzz.fuzz.ratio`` ≥ ``agent_fuzzy_min_ratio`` (default 80).
Chatter ("give me", "please"), colours, sizes and materials are never fuzzy-matched. Pure functions: no DB, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from app.config import get_settings
from app.services import indic_vocab

# Roman-script spellings of the same garment → the canonical English word a catalogue would use.
ALIASES: dict[str, str] = {
    **{w: "lehenga" for w in (
        "lahenga", "lehnga", "lahemgha", "lengha", "lehanga", "lahnga", "lehngha", "lahengha", "lenga", "lehengha", "lehengaa",
        "lahangha", "lehangha", "lehgna", "lehnaga", "lehegna", "lahegha", "lehenge", "lahengaa", "lehengas", "lehngas", "lahengas",
    )},
    **{w: "suit" for w in ("suite", "sut", "soot", "suits", "suites", "sutes")},
    **{w: "saree" for w in ("sari", "saari", "saaree", "sadi", "sadee", "saris", "sarees", "sarii", "saarii", "sarri", "sare")},
    **{w: "dupatta" for w in ("chunni", "chunri", "chunari", "dupata", "dupta", "dupattas", "chunnis", "odhni", "odhani", "chundadi")},
    **{w: "kurti" for w in ("kurtis", "kurthi", "kurty", "kurtee", "kurtie", "kurtiyan", "kurtiya")},
    **{w: "choli" for w in ("cholie", "choly", "cholee", "choli's", "blouse")},
    **{w: "palazzo" for w in ("plazo", "palazo", "palazzos", "plazzo", "palaazo", "palaso")},
    **{w: "salwar" for w in ("shalwar", "salvar", "shalvar", "salwaar")},
    **{w: "anarkali" for w in ("anarkli", "anarkalli", "anaarkali", "anarkalee")},
    **{w: "chaniya" for w in ("chania", "chaniyo", "ghagra", "ghaghra", "ghagri")},
}
# Words that must never be fuzzy-matched into a product word: customer chatter and shop words that happen to look alike.
_CHATTER = frozenset("""
give show want need please tell send share bring have has see look looking what which where when how much many price rate cost
available stock catalogue catalog collection products product items item order buy take get let know list more other another new
your you the and for with from this that these those some any all me my our hello hii help thank thanks okay yes
""".split())
_MIN_FUZZY_LEN = 4


@dataclass(frozen=True)
class Vocabulary:
    """What one tenant's catalogue says: its words (product names + categories) and its category names (sold out ones included)."""

    words: frozenset[str] = frozenset()
    categories: tuple[str, ...] = ()
    attr_values: frozenset[str] = frozenset()          # colours / sizes / materials — never "corrected" into product words
    word_list: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def build(cls, product_texts: list[str], categories: list[str], attr_values: list[str]) -> "Vocabulary":
        """Index a tenant's product names, categories and variant values."""
        words = {w for text in (*product_texts, *categories) for w in indic_vocab.tokenize(text) if len(w) >= 3}
        cats = tuple(dict.fromkeys(c.strip() for c in categories if c and c.strip()))
        values = {w for v in attr_values for w in indic_vocab.tokenize(v)}
        return cls(words=frozenset(words), categories=cats, attr_values=frozenset(values), word_list=tuple(sorted(words)))


@dataclass(frozen=True)
class QueryMatch:
    """A normalised query: the corrected text, which words were corrected, and the categories it names."""

    text: str
    corrections: dict[str, str] = field(default_factory=dict)       # customer's word → the catalogue's word
    categories: tuple[str, ...] = ()


def _singular(word: str) -> str:
    """'sarees' → 'saree' (matching only)."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def correct_word(token: str, vocab: Vocabulary) -> str:
    """The catalogue word `token` stands for (itself when it already is one, or when nothing is close enough)."""
    if token in vocab.words or token in vocab.attr_values or token in _CHATTER:
        return token
    if not token.isascii() or not token.isalpha():
        return token
    single = _singular(token)
    if single in vocab.words:
        return single
    alias = ALIASES.get(token)
    if alias is not None:
        if alias in vocab.words:
            return alias
        if _singular(alias) in vocab.words:
            return _singular(alias)
        return token
    if len(token) < _MIN_FUZZY_LEN or not vocab.word_list:
        return token
    best = process.extractOne(token, vocab.word_list, scorer=fuzz.ratio, score_cutoff=get_settings().agent_fuzzy_min_ratio)
    return best[0] if best else token


def categories_named(words: list[str], vocab: Vocabulary) -> tuple[str, ...]:
    """
    The shop's categories that `words` (already corrected) name: a category whose words ARE the query's product words wins alone
    ("lehenga" → Lehenga, not Lehenga Choli); otherwise every category containing one of them ("suit" → Salwar Suit + Anarkali Suit).
    """
    query = {_singular(w) for w in words if w in vocab.words or _singular(w) in vocab.words}
    if not query:
        return ()
    scored: list[tuple[str, set[str]]] = []
    for cat in vocab.categories:
        cat_words = {_singular(w) for w in indic_vocab.tokenize(cat) if len(w) >= 3}
        if cat_words & query:
            scored.append((cat, cat_words))
    exact = [cat for cat, cat_words in scored if cat_words == query]
    return tuple(exact or (cat for cat, _ in scored))


def normalize(query: str, vocab: Vocabulary) -> QueryMatch:
    """Correct every word of `query` against the tenant's vocabulary and name the categories it refers to."""
    tokens = indic_vocab.tokenize(query or "")
    fixed: list[str] = []
    corrections: dict[str, str] = {}
    for tok in tokens:
        new = correct_word(tok, vocab)
        if new != tok:
            corrections[tok] = new
        fixed.append(new)
    text = " ".join(fixed)
    cats = categories_named(fixed, vocab)
    return QueryMatch(text=text if corrections else " ".join((query or "").split()), corrections=corrections, categories=cats)
