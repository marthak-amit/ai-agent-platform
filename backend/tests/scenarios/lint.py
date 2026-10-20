"""
Language lint for scenario replies: a reply written in one template language must not contain words that only ANOTHER language's
template table uses ("aapka" in a Gujarati reply, "chhe" in a Hindi one, Hindi / Gujarati words in an English one).

``foreign_words(text, lang)`` = the words of `text` that appear in some other language's table, but neither in the customer's own table,
nor in the English table (English product words are shared by every Roman language), nor in the shop's own data (`allowed`: product
names, categories, colours, customer-typed words). Native-script languages are checked for stray Latin words by
``agent_templates.problems`` at the table level; here only Roman-script and English replies are linted.
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.services import agent_templates

_WORD = re.compile(r"[a-z]{3,}")
_PLACEHOLDER = re.compile(r"\{\w+\}")
ROMAN = ("english", "gujarati_roman", "hindi_roman")


@lru_cache(maxsize=1)
def _vocab() -> dict[str, frozenset[str]]:
    """Words of every Roman-script table (placeholders removed)."""
    tables = agent_templates.load_all()
    return {lang: frozenset(_WORD.findall(_PLACEHOLDER.sub(" ", " ".join(tables[lang].values())).lower())) for lang in ROMAN}


def foreign_words(text: str, lang: str, allowed: frozenset[str] = frozenset()) -> set[str]:
    """Words in `text` that belong to a different Roman template language than `lang` (see the module docstring)."""
    if lang not in ROMAN:
        return set()
    vocab = _vocab()
    own, english = vocab[lang], vocab["english"]
    others = frozenset().union(*(v for k, v in vocab.items() if k != lang))
    words = set(_WORD.findall(re.sub(r"https?://\S+", " ", text).lower()))
    return {w for w in words if w in others and w not in own and w not in english and w not in allowed}
