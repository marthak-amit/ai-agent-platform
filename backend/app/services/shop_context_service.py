"""
Shop profile — what THIS business sells, as a short cached text + keyword set.

Why: the router and the off-topic check must refuse questions that have nothing to do with the shop
("Ahmedabad ma su vatavaran chhe?") while still answering the shop's own topics ("which saree for a
wedding?", "how do I wash silk?"). The same prompt serves a saree shop, a phone shop and a clinic, so the
shop-specific part is data, built here from rows that already exist — nothing per customer is hand-written:

    sells: <business_type> | about: <description> | categories: … | products: … | shop topics: <KB titles>

* **Built by code, no LLM** — exact, free, cannot invent a category.
* **Self-maintaining** — `ensure_fresh()` fingerprints business type/description + the catalogue's
  categories and names (the router already loads the active catalogue every turn, so the check is free).
  A changed fingerprint rebuilds the profile on the next message; stock and price edits don't change it.
  Knowledge-base topics are picked up by `invalidate()` (called by the KB endpoints) and by a TTL.
* **Fail-safe** — any error returns a freshly built, unsaved profile (or None); callers treat "no
  profile" as "can't judge scope" and never block a customer because of it.
* The keyword set is the ₹0 in-scope pre-check: a message sharing a word with it is never refused.

Persistence runs in a SAVEPOINT of the caller's session, so a failed write never poisons the pipeline's transaction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.services import indic_vocab

logger = logging.getLogger("app.services.shop_context_service")

MAX_CATEGORIES = 10
MAX_PRODUCT_NAMES = 15
MAX_NAMES_PER_CATEGORY = 3
MAX_KB_TOPICS = 8
MAX_KEYWORDS = 800
DESCRIPTION_CHARS = 200
NAME_CHARS = 40
TEXT_CHARS = 900          # ≈ 200 tokens hard ceiling for the prompt line
MIN_KEYWORD_LEN = 3

# Words that say nothing about WHAT a business sells (descriptions are full of them). Kept short on
# purpose: a generic word that slips through only makes a message look "in scope" (the safe direction).
_STOPWORDS = frozenset(
    """
    the and for with our your you are was this that have has from all any new best good great top more most
    shop store online products product items item sell selling sells seller buy buying order orders ordering
    quality price prices cost wholesale retail bulk business company brand brands collection collections
    available offer offers deal deals sale india indian best home delivery service services customer customers
    here there will can get give make made also very just only about what which who how why when where
    hai hain hain ke ki ka ko se mein me par aur ya nu ni na ma ne che chhe chhu
    """.split()
)


@dataclass(frozen=True)
class ShopProfile:
    """The prompt line, its in-scope keyword set and the input fingerprint."""

    text: str
    keywords: frozenset
    fingerprint: str

    def is_empty(self) -> bool:
        """True when the shop gave us nothing to go on (no type, description, catalogue or KB)."""
        return not self.keywords


# ── building ─────────────────────────────────────────────────────────────────

def _words(text: str | None) -> list[str]:
    """Keyword candidates of a free-text field: tokens ≥3 chars, not digits, not stopwords (any script)."""
    out = []
    for token in indic_vocab.tokenize(text or ""):
        if len(token) >= MIN_KEYWORD_LEN and not token.isdigit() and token not in _STOPWORDS:
            out.append(token)
    return out


def _forms(token: str) -> set[str]:
    """The token plus a crude singular/plural pair so "sarees" matches "saree" (Latin script only)."""
    forms = {token}
    if token.isascii():
        if token.endswith("ies") and len(token) > 4:
            forms.add(token[:-3] + "y")
        elif token.endswith("es") and len(token) > 4:
            forms.add(token[:-2])
        if token.endswith("s") and len(token) > 3:
            forms.add(token[:-1])
        elif len(token) > 3:
            forms.add(token + "s")
    return forms


def _active(products: list) -> list:
    """Active products only (the router passes active rows already; direct callers may not)."""
    return [p for p in products or [] if getattr(p, "is_active", True) is not False]


def fingerprint(client, products: list) -> str:
    """Stable hash of everything the profile is built from except KB topics (price/stock excluded on purpose)."""
    parts = [
        (getattr(client, "business_type", None) or "").strip().lower(),
        (getattr(client, "business_description", None) or "").strip().lower(),
    ]
    parts.extend(sorted(
        f"{(getattr(p, 'category', None) or '').strip().lower()}:{(getattr(p, 'name', None) or '').strip().lower()}"
        for p in _active(products)
    ))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _spread_names(products: list) -> list[str]:
    """Up to MAX_PRODUCT_NAMES distinct names, at most MAX_NAMES_PER_CATEGORY per category (variety over volume)."""
    per_cat: dict[str, int] = {}
    names: list[str] = []
    seen: set[str] = set()
    for p in _active(products):
        name = " ".join((getattr(p, "name", None) or "").split())[:NAME_CHARS]
        cat = (getattr(p, "category", None) or "").strip().lower()
        if not name or name.lower() in seen or per_cat.get(cat, 0) >= MAX_NAMES_PER_CATEGORY:
            continue
        seen.add(name.lower())
        per_cat[cat] = per_cat.get(cat, 0) + 1
        names.append(name)
        if len(names) >= MAX_PRODUCT_NAMES:
            break
    return names


def build_profile(client, products: list, kb_titles: list[str] | None = None) -> ShopProfile:
    """
    Build the profile from the business fields, the catalogue and the approved KB question titles.

    The text is the prompt line (capped); the keyword set covers EVERY active product name and category
    (not just the ones shown in the text), the business type/description words and the KB titles.
    """
    business_type = " ".join((getattr(client, "business_type", None) or "").split())
    description = " ".join((getattr(client, "business_description", None) or "").split())[:DESCRIPTION_CHARS]
    active = _active(products)

    categories: list[str] = []
    for p in active:
        cat = " ".join((getattr(p, "category", None) or "").split())
        if cat and cat.lower() not in {c.lower() for c in categories}:
            categories.append(cat)
        if len(categories) >= MAX_CATEGORIES:
            break
    titles = []
    for title in kb_titles or []:
        t = " ".join((title or "").split())[:60]
        if t and t.lower() not in {x.lower() for x in titles}:
            titles.append(t)
        if len(titles) >= MAX_KB_TOPICS:
            break

    segments = []
    if business_type:
        segments.append(f"sells: {business_type}")
    if description:
        segments.append(f"about: {description}")
    if categories:
        segments.append(f"categories: {', '.join(categories)}")
    names = _spread_names(products)
    if names:
        segments.append(f"products: {', '.join(names)}")
    if titles:
        segments.append(f"shop topics: {', '.join(titles)}")
    text = " | ".join(segments)[:TEXT_CHARS]

    counts: dict[str, int] = {}
    sources = [business_type, description, *titles]
    sources.extend((getattr(p, "category", None) or "") for p in active)
    sources.extend((getattr(p, "name", None) or "") for p in active)
    for source in sources:
        for word in _words(source):
            for form in _forms(word):
                counts[form] = counts.get(form, 0) + 1
    keywords = frozenset(sorted(counts, key=lambda w: (-counts[w], w))[:MAX_KEYWORDS])
    return ShopProfile(text=text, keywords=keywords, fingerprint=fingerprint(client, products))


# ── matching ─────────────────────────────────────────────────────────────────

def matches(profile: ShopProfile | None, text: str) -> bool:
    """
    True when the message shares a word with the shop's vocabulary (so it is about this shop).

    Native-script product words are mapped to their English catalogue words first ("સાડી" → "saree"),
    the same way the catalogue search does. No profile / empty profile → False ("can't tell").
    """
    if profile is None or profile.is_empty() or not (text or "").strip():
        return False
    tokens = set(indic_vocab.tokenize(text))
    for phrase in indic_vocab.expand_query(text):
        tokens.update(indic_vocab.tokenize(phrase))
    for token in tuple(tokens):
        tokens.update(_forms(token))
    return bool(tokens & profile.keywords)


# ── persistence / freshness ──────────────────────────────────────────────────

def _from_columns(client, fp: str) -> ShopProfile | None:
    """The stored profile when it was built for fingerprint `fp` and is younger than the TTL; else None."""
    text = getattr(client, "shop_context", None)
    stored_hash = getattr(client, "shop_context_hash", None)
    updated = getattr(client, "shop_context_updated_at", None)
    keywords = getattr(client, "shop_context_keywords", None)
    if not text or not stored_hash or stored_hash != fp or not isinstance(keywords, list) or updated is None:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - updated > timedelta(seconds=get_settings().shop_context_ttl_seconds):
        return None
    return ShopProfile(text=text, keywords=frozenset(keywords), fingerprint=fp)


async def _kb_titles(db, client_id: int) -> list[str]:
    """Question titles of the shop's active, approved KB entries (most used first)."""
    from app.models.knowledge_base import KnowledgeBase

    rows = await db.execute(
        select(KnowledgeBase.question)
        .where(
            KnowledgeBase.client_id == client_id,
            KnowledgeBase.is_active == True,   # noqa: E712
            KnowledgeBase.is_approved == True,  # noqa: E712
        )
        .order_by(KnowledgeBase.usage_count.desc(), KnowledgeBase.id)
        .limit(MAX_KB_TOPICS * 3)
    )
    return [q for (q,) in rows.all()]


async def _persist(db, client, profile: ShopProfile) -> None:
    """
    Write the profile to the client row inside a SAVEPOINT of the caller's session.

    The savepoint keeps a failed write from poisoning the pipeline's transaction; the outer transaction commits
    (or rolls back — then the next message simply rebuilds) with the rest of the turn. The loaded `client` object is
    mirrored without being marked dirty.
    """
    from app.models.client import Client

    values = dict(
        shop_context=profile.text, shop_context_keywords=sorted(profile.keywords),
        shop_context_hash=profile.fingerprint, shop_context_updated_at=datetime.now(timezone.utc),
    )
    async with db.begin_nested():
        await db.execute(update(Client).where(Client.id == client.id).values(**values))
    for attr, value in values.items():
        try:
            set_committed_value(client, attr, value)
        except Exception:  # noqa: BLE001 — a non-ORM stand-in object just gets plain attributes
            setattr(client, attr, value)


async def ensure_fresh(db, client, products: list) -> ShopProfile | None:
    """
    The client's profile, rebuilt when the catalogue / business fields changed or the stored copy is stale.

    `products` is the active catalogue the caller already loaded this turn. Never raises: on any failure it
    returns the best profile it could build (possibly unsaved) or None.
    """
    if client is None:
        return None
    try:
        fp = fingerprint(client, products)
        cached = _from_columns(client, fp)
        if cached is not None:
            return cached
        titles: list[str] = []
        try:
            async with db.begin_nested():     # a failed read must not leave the pipeline's transaction aborted
                titles = await _kb_titles(db, client.id)
        except Exception as exc:  # noqa: BLE001 — KB topics are a nicety
            logger.warning("shop_context: KB topics unavailable client=%s: %s", getattr(client, "id", "?"), exc)
        profile = build_profile(client, products, titles)
        try:
            await _persist(db, client, profile)
            logger.info(
                "shop_context rebuilt client=%s keywords=%d chars=%d", client.id, len(profile.keywords), len(profile.text),
            )
        except Exception as exc:  # noqa: BLE001 — serving an unsaved profile beats serving none
            logger.warning("shop_context: persist failed client=%s: %s", getattr(client, "id", "?"), exc)
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.warning("shop_context: build failed client=%s: %s", getattr(client, "id", "?"), exc)
        return None


async def invalidate(db, client_id: int) -> None:
    """Force a rebuild on the next message (a KB entry was added / edited / deleted / toggled). Never raises."""
    from app.models.client import Client

    try:
        await db.execute(update(Client).where(Client.id == client_id).values(shop_context_hash=None))
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("shop_context: invalidate failed client=%s: %s", client_id, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
