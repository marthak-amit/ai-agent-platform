"""
Agent v3 — the planner's and executor's read-only view of the catalogue: real variants, real stock.

``ProductView`` is built from an already-loaded ``Product`` (its variants are selectin-loaded) plus the units held by
other customers' unpaid orders (``stock_reservations``), so "in stock" here means what ``order_service`` would accept.
Everything the planner is told about a product (and every option the customer is offered) comes from here — never from
the model's memory.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.models.stock_reservation import RESERVATION_ACTIVE, StockReservation
from app.services import agent_query, catalogue_service, catalogue_snapshot, indic_vocab, turn_trace

logger = logging.getLogger("app.services.agent_catalog")

ATTRS: tuple[str, ...] = ("color", "size", "material")

# Shades that stand in for each other when the exact colour is not in stock ("No red saree. Closest: Maroon").
COLOR_FAMILIES: tuple[frozenset[str], ...] = tuple(frozenset(f.split()) for f in (
    "red maroon wine burgundy crimson rust brick cherry scarlet",
    "pink rose magenta fuchsia peach coral salmon blush",
    "orange mustard rust coral saffron tangerine peach",
    "yellow mustard gold golden lemon",
    "green olive mint teal lime emerald bottle sage",
    "blue navy sky teal turquoise indigo cyan royal",
    "purple violet lavender mauve plum wine lilac",
    "black charcoal grey gray slate",
    "white cream ivory beige offwhite",
    "brown tan beige coffee chocolate camel khaki",
))
SIZE_LADDER: tuple[str, ...] = ("xxs", "xs", "s", "m", "l", "xl", "xxl", "3xl", "xxxl", "4xl", "5xl")
_BIG_SIZES = frozenset({"xs", "xl", "xxl", "xxxl", "3xl", "4xl", "5xl"})        # words that are only ever sizes


@dataclass
class VariantView:
    """One sellable variant: its attributes, price and the units a NEW order could still take."""

    id: int | None
    color: str | None
    size: str | None
    material: str | None
    price: float | None
    available: int | None      # None = unlimited (product tracks no stock)
    image_url: str | None = None

    def attr(self, name: str) -> str | None:
        """The variant's value for 'color' / 'size' / 'material'."""
        return getattr(self, name)

    def matches(self, fixed: dict[str, str | None]) -> bool:
        """True when every fixed attribute equals this variant's (case-insensitive); ``None`` fixes nothing."""
        return all(v is None or _norm(self.attr(k)) == _norm(v) for k, v in fixed.items())


def _norm(value: str | None) -> str:
    """Comparison form of a catalogue value."""
    return " ".join((value or "").lower().split())


@dataclass
class ProductView:
    """A product with its active variants. ``variants`` is empty for a product without variants."""

    product: object
    variants: list[VariantView] = field(default_factory=list)

    @property
    def sku(self) -> str:
        """The product's SKU (upper-case as stored)."""
        return self.product.sku or ""

    @property
    def name(self) -> str:
        """Product name."""
        return self.product.name

    @property
    def price(self) -> float:
        """Base price (a variant may override it, see ``price_for``)."""
        return float(self.product.price or 0)

    @property
    def has_variants(self) -> bool:
        """True when the product is sold in variants."""
        return bool(self.variants)

    @property
    def needed_attrs(self) -> list[str]:
        """Attributes the customer must choose: those that at least one variant defines (colour, size, material)."""
        return [a for a in ATTRS if any(v.attr(a) for v in self.variants)]

    def values(self, attr: str, fixed: dict[str, str | None] | None = None, *, in_stock_only: bool = True) -> list[str]:
        """Distinct values of `attr` (catalogue order) among variants matching `fixed`; in-stock ones unless told otherwise."""
        seen: dict[str, str] = {}
        for v in self.variants:
            if not v.attr(attr) or not v.matches(fixed or {}):
                continue
            if in_stock_only and v.available is not None and v.available <= 0:
                continue
            seen.setdefault(_norm(v.attr(attr)), v.attr(attr))
        return list(seen.values())

    def available(self, fixed: dict[str, str | None]) -> int | None:
        """Units a new order could take for the variants matching `fixed` (None = unlimited / untracked)."""
        if not self.variants:
            stock = self.product.stock
            return None if stock is None else max(0, int(stock))
        matching = [v for v in self.variants if v.matches(fixed)]
        if any(v.available is None for v in matching):
            return None
        return sum(max(0, int(v.available or 0)) for v in matching)

    def variant_for(self, fixed: dict[str, str | None]) -> VariantView | None:
        """The (first) variant matching `fixed` — only meaningful once every needed attribute is fixed."""
        for v in self.variants:
            if v.matches(fixed):
                return v
        return None

    def unit_price(self, fixed: dict[str, str | None]) -> float:
        """Price of one unit of the chosen variant (variant override, else the product price)."""
        v = self.variant_for(fixed) if self.variants else None
        return float(v.price) if v is not None and v.price else self.price

    def in_stock(self) -> bool:
        """True when at least one unit can be ordered."""
        avail = self.available({})
        return avail is None or avail > 0

    def resolve(self, attr: str, text: str | None) -> list[str]:
        """
        Catalogue values of `attr` (in or out of stock) that `text` names: exact (case-insensitive), then via the shared
        colour vocabulary ("gulabi" → Pink), then a close spelling ("pinkk"). One entry = unambiguous.
        """
        text = " ".join((text or "").split())
        if not text:
            return []
        everything = self.values(attr, in_stock_only=False)
        exact = [v for v in everything if _norm(v) == _norm(text)]
        if exact:
            return exact[:1]
        if attr == "color":
            wanted = indic_vocab.canonical_color(text)
            if wanted:
                by_alias = [v for v in everything if wanted in (indic_vocab.canonical_colors(v) or [_norm(v)])]
                if by_alias:
                    return by_alias
        close = difflib.get_close_matches(_norm(text), [_norm(v) for v in everything], n=1, cutoff=0.82)
        return [v for v in everything if _norm(v) == close[0]][:1] if close else []


async def _reserved_by_variant(db, product_ids: list[int]) -> dict[tuple[int, int | None], int]:
    """Active reservation totals keyed by (product_id, variant_id) for the given products (one query)."""
    if not product_ids:
        return {}
    rows = await db.execute(
        select(StockReservation.product_id, StockReservation.variant_id, func.coalesce(func.sum(StockReservation.quantity), 0))
        .where(StockReservation.product_id.in_(product_ids), StockReservation.status == RESERVATION_ACTIVE)
        .group_by(StockReservation.product_id, StockReservation.variant_id)
    )
    return {(pid, vid): int(qty) for pid, vid, qty in rows.all()}


def _canon_colors(value: str) -> set[str]:
    """Colour names `value` stands for: itself (lower-case) plus the shared vocabulary's English name ("laal" → red)."""
    names = {value.strip().lower()} if value else set()
    names.update(indic_vocab.canonical_colors(value))
    return names


def parse_attributes(query: str, catalogue: "Catalogue") -> tuple[str, dict[str, str]]:
    """
    Split a catalogue query into the product words and the variant filters it contains.

    "red kurti" → ("kurti", {"color": "red"}); "xl lehenga" → ("lehenga", {"size": "XL"}); "blue m lehenga" → ("lehenga",
    {"color": "blue", "size": "M"}); "laal kurti" → ("kurti", {"color": "red"}). Colours: the shared multilingual vocabulary or
    a colour this shop's variants have. Sizes: xl / xxl / 3xl … always; a single letter (s / m / l) only when some variant has
    that size (so "men's" is not a size). Materials: values the shop's variants have.
    """
    colors = {c.lower() for v in catalogue.views for c in v.values("color", in_stock_only=False)}
    sizes = {z.lower() for v in catalogue.views for z in v.values("size", in_stock_only=False)}
    materials = {m.lower() for v in catalogue.views for m in v.values("material", in_stock_only=False)}
    filters: dict[str, str] = {}
    rest: list[str] = []
    for tok in indic_vocab.tokenize(query):
        english = indic_vocab.canonical_colors(tok)
        if "color" not in filters and (tok in colors or english):
            filters["color"] = tok if tok in colors else english[0]
        elif "size" not in filters and (tok in _BIG_SIZES or (len(tok) == 1 and tok in sizes) or (tok in sizes and tok.isascii() and len(tok) <= 4)):
            filters["size"] = tok.upper()
        elif "material" not in filters and tok in materials:
            filters["material"] = tok
        else:
            rest.append(tok)
    return " ".join(rest), filters


def with_singulars(text: str) -> str:
    """
    `text` plus the singular of every plural-looking word ("sarees" → saree, "kurtis" → kurti, "dresses" → dress), so a
    plural customer word still matches a singular product name. Only used for matching, never shown.
    """
    extra: list[str] = []
    for word in indic_vocab.tokenize(text):
        if not word.isascii() or len(word) <= 3:
            continue
        if word.endswith("ies"):
            extra.append(word[:-3] + "y")
        elif word.endswith("es") and word[-3] in "sxz":
            extra.append(word[:-2])
        elif word.endswith("s") and not word.endswith("ss"):
            extra.append(word[:-1])
    return f"{text} {' '.join(extra)}" if extra else text


def _eq_ci(a: str | None, b: str | None) -> bool:
    """Case-insensitive equality."""
    return _norm(a) == _norm(b)


def _variant_matches(view: ProductView, variant: VariantView, filters: dict[str, str]) -> bool:
    """Does this variant satisfy every parsed filter (colour via the vocabulary, size / material exactly)?"""
    for attr, wanted in filters.items():
        value = variant.attr(attr)
        if not value:
            return False
        if attr == "color":
            if not (_canon_colors(wanted) & _canon_colors(value)) and _norm(value) != _norm(wanted):
                return False
        elif _norm(value) != _norm(wanted):
            return False
    return True


#: client_id → (the snapshot rows it was built from, stock/reservation signature, the Catalogue) — see ``Catalogue.load``.
_catalogue_cache: dict[int, tuple[object, tuple, "Catalogue"]] = {}


class Catalogue:
    """The tenant's active products as ``ProductView``s, loaded once per turn (and reused while nothing it is built from changed)."""

    def __init__(self, views: list[ProductView]) -> None:
        """Index the views by upper-case SKU."""
        self.views = views
        self._by_sku = {v.sku.upper(): v for v in views if v.sku}
        self._vocab: set[str] | None = None
        self._by_name: dict[str, ProductView | None] | None = None
        self._fuzzy_vocab: agent_query.Vocabulary | None = None
        self._normalized: dict[str, agent_query.QueryMatch] = {}

    @classmethod
    async def load(cls, db, client_id: int) -> "Catalogue":
        """
        The tenant's active products as views: static rows from the process-wide snapshot (``catalogue_snapshot``, ~1 minute, dropped
        on every catalogue write), stock and reservations re-read from the database on EVERY call.
        """
        with turn_trace.span("search"):
            rows = catalogue_snapshot.get(client_id)
            if rows is None:
                rows = catalogue_snapshot.get_stale(client_id)          # past its TTL: answer from it now, reload in the background
                if rows is not None:
                    catalogue_snapshot.refresh_soon(client_id, db)
                else:
                    rows = await catalogue_snapshot.load_rows(db, client_id)
            variant_stock, product_stock = await catalogue_snapshot.load_stock(db, client_id)
            reserved = await _reserved_by_variant(db, [r.id for r in rows])
            # Stock is re-read every turn, but the OBJECTS built from it (thousands of views, the fuzzy vocabulary, the name map) are
            # reused while neither the static rows nor any stock / reservation number changed — rebuilding them per message was ~25 ms of
            # synchronous CPU on the event loop for a 1,000-product shop and a steady stream of garbage for the collector.
            signature = (frozenset(variant_stock.items()), frozenset(product_stock.items()), frozenset(reserved.items()))
            cached = _catalogue_cache.get(client_id)
            if cached is not None and cached[0] is rows and cached[1] == signature:
                return cached[2]
            reserved_by_product: dict[int, int] = {}
            for (pid, _vid), qty in reserved.items():
                reserved_by_product[pid] = reserved_by_product.get(pid, 0) + qty
            views: list[ProductView] = []
            for r in rows:
                if r.has_variants:
                    variants = [
                        VariantView(
                            id=v.id, color=v.color, size=v.size, material=v.material, price=v.price,
                            available=max(0, variant_stock.get(v.id, 0) - reserved.get((r.id, v.id), 0)), image_url=v.image_url,
                        )
                        for v in r.variants
                    ]
                    stock = None
                else:
                    variants = []
                    raw = product_stock.get(r.id)
                    stock = None if raw is None else max(0, raw - reserved_by_product.get(r.id, 0))
                views.append(ProductView(product=ProductSnap(r, stock), variants=variants))
            catalogue = cls(views)
            _catalogue_cache[client_id] = (rows, signature, catalogue)
            return catalogue

    def get(self, sku: str | None) -> ProductView | None:
        """Exact (case-insensitive) SKU lookup — no prefix guessing, a hallucinated SKU must not resolve."""
        return self._by_sku.get((sku or "").strip().upper())

    def get_by_name(self, text: str) -> ProductView | None:
        """The product whose whole name is `text` (case / punctuation / spacing ignored); None when none or several match."""
        if self._by_name is None:
            self._by_name = {}
            for v in self.views:
                key = catalogue_snapshot.normalize_name(v.name)
                self._by_name[key] = None if key in self._by_name else v
        return self._by_name.get(catalogue_snapshot.normalize_name(text))

    def search(self, query: str, top_k: int = 8) -> list[ProductView]:
        """Keyword search (names / categories / descriptions, in any script), best first, in-stock products before sold-out."""
        query = self.normalize_query(query).text
        with turn_trace.span("search"):
            scored = catalogue_service.search_products_with_scores([v.product for v in self.views], with_singulars(query or ""), top_k=top_k * 2)
        picked = [self.get(getattr(p, "sku", None) or "") or self._view_of(p) for _score, p in scored]
        picked = [v for v in picked if v is not None]
        picked.sort(key=lambda v: not v.in_stock())      # stable: keeps the relevance order within each group
        return picked[:top_k]

    def search_filtered(self, query: str, top_k: int = 5) -> dict:
        """
        Search with the variant filters parsed out of the query ("red kurti").

        Returns ``{"views", "labels", "filters", "text", "closest"}``: `views` = products with an in-stock variant matching EVERY
        filter (`labels` = those variants, e.g. ["Blue / M"]); when none match, `closest` = ``{"attr", "values", "views"}`` —
        the same kind of product in the nearest colour family / size, or every available value — so the reply never says
        "not available" without an alternative. Without filters it is the plain keyword search.
        """
        query = self.normalize_query(query).text
        text, filters = parse_attributes(query, self)
        if not filters:
            return {"views": self.search(query, top_k), "labels": {}, "filters": {}, "text": query, "closest": None}
        # Filter FIRST, over every product (a colour is matched against variant colour values only — never fuzzily against names —
        # and never after cutting the list to the best name matches), then rank what is left by the remaining words.
        candidates = [v for v in self.views if v.in_stock() and any(self._in_stock_match(v, var, filters) for var in v.variants)]
        if text:
            with turn_trace.span("search"):
                scored = catalogue_service.search_products_with_scores([v.product for v in candidates], with_singulars(text), top_k=len(candidates))
            by_product = {id(v.product): v for v in candidates}
            candidates = [by_product[id(p)] for _score, p in scored if id(p) in by_product]
        labels: dict[str, list[str]] = {}
        for view in candidates:
            hits = [var for var in view.variants if self._in_stock_match(view, var, filters)]
            labels[view.sku] = list(dict.fromkeys(" / ".join(str(var.attr(a)) for a in view.needed_attrs if var.attr(a)) for var in hits))
        if candidates:
            return {"views": candidates[:top_k], "labels": labels, "filters": filters, "text": text, "closest": None}
        base = self.search(text, 8) if text else [v for v in self.views if v.in_stock()][:8]
        return {"views": [], "labels": {}, "filters": filters, "text": text, "closest": self._closest(base, filters)}

    @staticmethod
    def _in_stock_match(view: ProductView, var: VariantView, filters: dict[str, str]) -> bool:
        """An orderable (in-stock) variant that satisfies every filter."""
        return (var.available is None or var.available > 0) and _variant_matches(view, var, filters)

    def _closest(self, base: list[ProductView], filters: dict[str, str]) -> dict | None:
        """The nearest real alternative to an unmatched filter among `base` (None when there is no such product at all)."""
        in_stock = [v for v in base if v.in_stock() and v.has_variants]
        if not in_stock:
            return None
        attr = next(a for a in ATTRS if a in filters)
        available: dict[str, str] = {}
        for v in in_stock:
            for val in v.values(attr):
                available.setdefault(val.lower(), val)
        if not available:
            return None
        wanted = filters[attr].lower()
        values = list(available.values())
        if attr == "color":
            family = next((f for f in COLOR_FAMILIES if _canon_colors(wanted) & f), frozenset())
            near = [val for val in values if _canon_colors(val) & family or val.lower() in family]
            values = near or values
        elif attr == "size" and wanted in SIZE_LADDER:
            ranked = sorted((abs(SIZE_LADDER.index(val.lower()) - SIZE_LADDER.index(wanted)), val) for val in values if val.lower() in SIZE_LADDER)
            if ranked:
                best = ranked[0][0]
                values = [val for dist, val in ranked if dist == best]
        views = [v for v in in_stock if any(_eq_ci(x, val) for x in v.values(attr) for val in values)]
        return {"attr": attr, "values": values, "views": views}

    def _view_of(self, product) -> ProductView | None:
        """The view wrapping `product` (a SKU-less product can't be found by SKU)."""
        for v in self.views:
            if v.product is product:
                return v
        return None

    def vocabulary(self) -> set[str]:
        """Every word of the shop's product names and categories (any script → English product words included)."""
        if self._vocab is None:
            words: set[str] = set()
            for v in self.views:
                words |= {w for w in catalogue_service._tokenize(f"{v.product.name} {v.product.category or ''}") if len(w) >= 3}
            self._vocab = words
        return self._vocab

    def mentions(self, text: str) -> bool:
        """True when the message names a product or category of this shop ("choli", "kurti", "saree", "सारी")."""
        text = self.normalize_query(text).text
        with turn_trace.span("search"):
            return bool({w for w in catalogue_service._tokenize(with_singulars(text)) if len(w) >= 3} & self.vocabulary())

    def keyword_hit(self, text: str) -> bool:
        """True when a keyword search on the NAME / CATEGORY words of `text` finds at least one product (score ≥ 2)."""
        text = self.normalize_query(text or "").text
        with turn_trace.span("search"):
            hits = catalogue_service.search_products_with_scores([v.product for v in self.views], with_singulars(text), top_k=3)
        return any(score >= 2 for score, _p in hits)

    def categories(self) -> list[str]:
        """Distinct non-empty categories of in-stock products (catalogue order)."""
        seen: dict[str, str] = {}
        for v in self.views:
            cat = (v.product.category or "").strip()
            if cat and v.in_stock():
                seen.setdefault(cat.lower(), cat)
        return list(seen.values())

    def in_category(self, category: str) -> list[ProductView]:
        """
        Products whose category matches `category`: exactly (case-insensitive / shared category vocabulary), else the categories the
        words name after spelling and transliteration are fixed ("lahemgha" → Lehenga, "suit" → Salwar Suit + Anarkali Suit).
        """
        wanted = {_norm(category), *(_norm(c) for c in indic_vocab.expand_query(category))}
        exact = [v for v in self.views if _norm(v.product.category) in wanted]
        if exact:
            return exact
        named = {_norm(c) for c in self.find_categories(category)}
        return [v for v in self.views if _norm(v.product.category) in named] if named else []

    # ── spelling / transliteration tolerance (tenant-scoped: only THIS catalogue's words are ever used) ──
    def fuzzy_vocabulary(self) -> agent_query.Vocabulary:
        """This shop's product-name words, categories (sold-out ones too) and variant values."""
        if self._fuzzy_vocab is None:
            self._fuzzy_vocab = agent_query.Vocabulary.build(
                [v.name for v in self.views],
                [v.product.category for v in self.views if v.product.category],
                [val for v in self.views for var in v.variants for val in (var.color, var.size, var.material) if val],
            )
        return self._fuzzy_vocab

    def normalize_query(self, text: str | None) -> agent_query.QueryMatch:
        """`text` with each misspelt / transliterated word replaced by the shop's own word (memoised per turn)."""
        key = text or ""
        hit = self._normalized.get(key)
        if hit is None:
            hit = self._normalized[key] = agent_query.normalize(key, self.fuzzy_vocabulary())
        return hit

    def find_categories(self, text: str | None) -> list[str]:
        """The shop's categories the words of `text` name (spelling-tolerant), any stock level; [] when none."""
        return list(self.normalize_query(text).categories)

    def all_categories(self) -> list[str]:
        """Distinct non-empty categories, sold-out ones included (catalogue order)."""
        seen: dict[str, str] = {}
        for v in self.views:
            cat = (v.product.category or "").strip()
            if cat:
                seen.setdefault(cat.lower(), cat)
        return list(seen.values())


class ProductSnap:
    """A product as the agent reads it: the cached static row plus this turn's stock (``None`` = untracked / unlimited)."""

    is_active = True

    def __init__(self, row: "catalogue_snapshot.ProductRow", stock: int | None) -> None:
        """Copy the row's attributes the agent reads."""
        self.id, self.sku, self.name, self.price = row.id, row.sku, row.name, row.price
        self.category, self.description, self.image_url = row.category, row.description, row.image_url
        self.delivery_days, self.has_variants, self.stock = row.delivery_days, row.has_variants, stock
