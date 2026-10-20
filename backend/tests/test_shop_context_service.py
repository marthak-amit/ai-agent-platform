"""Unit tests for the shop profile (no DB): fingerprint, build, in-scope matching, freshness, caps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import shop_context_service as scs


def prod(name, category, *, price=500.0, stock=5, active=True):
    """Fake Product row."""
    return SimpleNamespace(name=name, category=category, price=price, stock=stock, is_active=active)


def shop(**kw):
    """Fake Client row with the profile columns unset."""
    base = dict(
        id=1, business_type="clothing", business_description="Ethnic wear: sarees and lehengas",
        shop_context=None, shop_context_keywords=None, shop_context_hash=None, shop_context_updated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


CATALOGUE = [
    prod("Green Cotton Saree", "Saree"), prod("Red Silk Saree", "Saree"), prod("Cotton Lehenga", "Lehenga"),
    prod("Printed Kurti", "Kurti"),
]


# ── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_ignores_price_and_stock_but_not_names_categories_or_description():
    """Stock/price edits keep the profile; a new product, rename, category or description change rebuilds it."""
    base = scs.fingerprint(shop(), CATALOGUE)
    cheaper = [prod(p.name, p.category, price=1.0, stock=0) for p in CATALOGUE]
    assert scs.fingerprint(shop(), cheaper) == base
    assert scs.fingerprint(shop(), CATALOGUE + [prod("Denim Jacket", "Jacket")]) != base
    renamed = [prod("Green Cotton Saree XL", "Saree")] + CATALOGUE[1:]
    assert scs.fingerprint(shop(), renamed) != base
    assert scs.fingerprint(shop(business_description="Phones"), CATALOGUE) != base
    assert scs.fingerprint(shop(business_type="electronics"), CATALOGUE) != base


def test_fingerprint_is_order_independent_and_skips_inactive_products():
    """Listing order doesn't matter; an inactive product is not part of what the shop sells."""
    base = scs.fingerprint(shop(), CATALOGUE)
    assert scs.fingerprint(shop(), list(reversed(CATALOGUE))) == base
    assert scs.fingerprint(shop(), CATALOGUE + [prod("Old Stock", "Saree", active=False)]) == base


# ── build ────────────────────────────────────────────────────────────────────

def test_build_profile_text_lists_type_description_categories_products_and_kb_topics():
    """The prompt line carries what the business offers, capped and without prices."""
    profile = scs.build_profile(shop(), CATALOGUE, ["Return policy", "Delivery charges"])
    assert profile.text.startswith("sells: clothing | about: Ethnic wear")
    for part in ("categories: Saree, Lehenga, Kurti", "Green Cotton Saree", "shop topics: Return policy, Delivery charges"):
        assert part in profile.text
    assert "₹" not in profile.text and "500" not in profile.text


def test_build_profile_caps_names_per_category_and_total_size():
    """200 sarees do not flood the prompt: ≤3 names per category, ≤15 overall, text ≤ the hard ceiling."""
    many = [prod(f"Saree Model {i}", "Saree") for i in range(200)] + [prod(f"Kurti {i}", f"Cat{i}") for i in range(40)]
    profile = scs.build_profile(shop(), many, [f"Topic number {i}" for i in range(30)])
    names_part = profile.text.split("products: ")[1].split(" | ")[0].split(", ")
    assert len(names_part) <= scs.MAX_PRODUCT_NAMES
    assert sum(n.startswith("Saree Model") for n in names_part) <= scs.MAX_NAMES_PER_CATEGORY
    assert len(profile.text) <= scs.TEXT_CHARS
    assert len(profile.keywords) <= scs.MAX_KEYWORDS


def test_keywords_cover_every_product_not_only_those_shown_in_the_text():
    """A product beyond the 15 displayed names is still in the keyword set (the in-scope pre-check sees the whole catalogue)."""
    many = [prod(f"Model{i} Banarasi", "Saree") for i in range(40)] + [prod("Zebrawood Table", "Furniture")]
    profile = scs.build_profile(shop(), many)
    assert "zebrawood" in profile.keywords and "furniture" in profile.keywords


def test_empty_shop_builds_an_empty_profile_that_never_matches():
    """No type, description or catalogue → empty keywords; matches() says 'cannot tell', never 'in scope'."""
    profile = scs.build_profile(shop(business_type=None, business_description=None), [])
    assert profile.is_empty() and profile.text == ""
    assert scs.matches(profile, "saree") is False and scs.matches(None, "saree") is False


def test_stopwords_and_short_words_are_not_keywords():
    """Generic description words ('best', 'quality', 'shop') must not make everything look in scope."""
    profile = scs.build_profile(shop(business_description="Best quality shop online store for all"), [])
    assert not ({"best", "quality", "shop", "online", "store", "for", "all"} & profile.keywords)


# ── matching ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("do you have sarees", True),                 # plural → singular
    ("Silk saree kaise dhoye", True),             # Roman Hindi around an English product word
    ("lehenga under 5000", True),
    ("સાડી છે?", True),                           # Gujarati script → 'saree'
    ("ચણિયા ચોળી પણ વેચો છો?", False),              # catalogue has no chaniya choli → not matched by words alone
    ("साड़ी दिखाओ", True),                         # Devanagari
    ("Ahmedabad ma su vatavaran chr", False),     # the weather question
    ("who won the cricket match", False),
    ("tell me a joke", False),
    ("", False),
])
def test_matches_uses_the_shops_own_vocabulary(text, expected):
    """A message sharing a word with the catalogue / business fields is about this shop; weather/cricket/jokes are not."""
    assert scs.matches(scs.build_profile(shop(), CATALOGUE), text) is expected


def test_matches_follows_each_shops_own_catalogue():
    """The same sentence is in scope for one shop and not for another — no per-customer rules."""
    phones = shop(business_type="electronics", business_description="Mobile phones and chargers")
    phone_profile = scs.build_profile(phones, [prod("Galaxy Phone X", "Phone"), prod("Fast Charger", "Charger")])
    saree_profile = scs.build_profile(shop(), CATALOGUE)
    assert scs.matches(phone_profile, "which phone is best for gaming") is True
    assert scs.matches(saree_profile, "which phone is best for gaming") is False
    assert scs.matches(saree_profile, "which saree for a wedding") is True
    assert scs.matches(phone_profile, "which saree for a wedding") is False


def test_kb_topics_make_policy_questions_in_scope():
    """A seller's KB titles ('Return policy') are part of the vocabulary."""
    profile = scs.build_profile(shop(business_description=None), [], ["Return policy", "Gift wrapping"])
    assert scs.matches(profile, "what is your return rules") is True
    assert scs.matches(profile, "do you do gift wrapping") is True


# ── freshness ────────────────────────────────────────────────────────────────

def _stored(client, profile, *, age_seconds=10):
    """Mirror a built profile onto the client's columns as _persist would."""
    client.shop_context = profile.text
    client.shop_context_keywords = sorted(profile.keywords)
    client.shop_context_hash = profile.fingerprint
    client.shop_context_updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)


def test_from_columns_returns_the_cached_profile_only_when_hash_matches_and_not_stale(monkeypatch):
    """Cache hit = same fingerprint and younger than the TTL; a changed catalogue or an old copy is a miss."""
    monkeypatch.setattr(scs, "get_settings", lambda: SimpleNamespace(shop_context_ttl_seconds=1800))
    client = shop()
    profile = scs.build_profile(client, CATALOGUE, ["Return policy"])
    _stored(client, profile)
    fp = scs.fingerprint(client, CATALOGUE)
    hit = scs._from_columns(client, fp)
    assert hit is not None and hit.text == profile.text and hit.keywords == profile.keywords
    assert scs._from_columns(client, scs.fingerprint(client, CATALOGUE + [prod("New", "New")])) is None
    _stored(client, profile, age_seconds=3600)
    assert scs._from_columns(client, fp) is None
    client.shop_context_hash = None          # invalidate()
    assert scs._from_columns(client, fp) is None


class _FakeDB:
    """Stands in for AsyncSession: counts executes; begin_nested() is a no-op context manager."""

    def __init__(self):
        """No statements yet."""
        self.executed = 0

    def begin_nested(self):
        """Async context manager that does nothing."""
        class _Ctx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    async def execute(self, *_a, **_k):
        """Count the UPDATE."""
        self.executed += 1


async def test_ensure_fresh_builds_persists_then_serves_from_cache(monkeypatch):
    """First call builds + saves; the second (same catalogue) reuses it with no KB query; a catalogue edit rebuilds."""
    monkeypatch.setattr(scs, "get_settings", lambda: SimpleNamespace(shop_context_ttl_seconds=1800))
    kb_calls = []

    async def fake_titles(db, client_id):
        """Record the KB read."""
        kb_calls.append(client_id)
        return ["Return policy"]

    monkeypatch.setattr(scs, "_kb_titles", fake_titles)
    client, db = shop(), _FakeDB()
    first = await scs.ensure_fresh(db, client, CATALOGUE)
    assert "shop topics: Return policy" in first.text and db.executed == 1 and kb_calls == [1]
    assert client.shop_context == first.text                      # mirrored on the loaded row

    again = await scs.ensure_fresh(db, client, CATALOGUE)
    assert again.text == first.text and db.executed == 1 and kb_calls == [1]

    rebuilt = await scs.ensure_fresh(db, client, CATALOGUE + [prod("Denim Jacket", "Jacket")])
    assert "Jacket" in rebuilt.text and db.executed == 2 and kb_calls == [1, 1]


async def test_ensure_fresh_survives_kb_and_persist_failures(monkeypatch):
    """A broken KB read or UPDATE still yields a usable (unsaved) profile — never an exception, never None."""
    monkeypatch.setattr(scs, "get_settings", lambda: SimpleNamespace(shop_context_ttl_seconds=1800))

    async def boom(*_a, **_k):
        """Fail like a dead DB."""
        raise RuntimeError("db down")

    class _BadDB(_FakeDB):
        async def execute(self, *_a, **_k):
            """Fail the UPDATE."""
            raise RuntimeError("db down")

    monkeypatch.setattr(scs, "_kb_titles", boom)
    profile = await scs.ensure_fresh(_BadDB(), shop(), CATALOGUE)
    assert profile is not None and "Saree" in profile.text and "shop topics" not in profile.text


async def test_ensure_fresh_with_no_client_returns_none():
    """No client → no profile (callers then skip the scope decision)."""
    assert await scs.ensure_fresh(_FakeDB(), None, CATALOGUE) is None


async def test_invalidate_never_raises():
    """A failing UPDATE/commit is swallowed (the TTL covers it)."""
    class _DB:
        async def execute(self, *_a, **_k):
            """Fail."""
            raise RuntimeError("nope")

        async def rollback(self):
            """Rollback is also best-effort."""
            raise RuntimeError("nope")

    await scs.invalidate(_DB(), 1)
