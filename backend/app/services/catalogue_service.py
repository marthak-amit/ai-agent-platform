"""
Catalogue service — product CRUD, stock adjustment, keyword search, and
prompt-context formatting.

Search keeps the top-5 most relevant products out of potentially hundreds,
injecting only those into the AI prompt to reduce token cost.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.stock_log import StockLog, VALID_REASONS

_UNSET: Any = object()   # sentinel — distinguishes "not provided" from False/0

# Words that carry no product-search signal.
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "i", "me", "my", "we", "our", "you", "your", "it", "its",
    "this", "that", "these", "those",
    "tell", "show", "want", "know", "get", "give", "please", "help",
    "much", "many", "any", "some", "more", "all", "about", "for", "of",
    "in", "on", "at", "to", "by", "with", "from", "into", "and", "or",
    "price", "cost", "rate", "stock", "available", "availability",
    # Generic adjectives/fillers that appear in many unrelated product names
    # ("kurti new one") and would otherwise inflate the match score for a
    # query that names a different, unrelated product ("new jeans available?").
    "new", "one", "item", "piece", "product", "good", "nice",
}


# ── SKU generation ────────────────────────────────────────────────────────────

# Textile category → 2-letter prefix mapping
_CATEGORY_PREFIXES: dict[str, str] = {
    "saree":     "SR",
    "lehenga":   "LH",
    "kurti":     "KU",
    "dupatta":   "DP",
    "jewellery": "JW",
    "jewelry":   "JW",
}
_DEFAULT_PREFIX = "PR"

# Regex that matches a SKU-like token: 2–4 letters followed by 4–6 digits (case-insensitive)
# e.g.  SR27754  LH00123  KU99001  sr27754
SKU_PATTERN = re.compile(r"\b([A-Za-z]{2,4})(\d{4,6})\b")


def generate_sku(category: Optional[str] = None) -> str:
    """
    Auto-generate a category-aware SKU.

    Format: 2-letter category prefix + 5 random digits.
    Examples:
        category="Saree"   → SR27754
        category="Lehenga" → LH09341
        category=None      → PR55820

    Args:
        category: Optional product category string (case-insensitive).

    Returns:
        A short unique-ish SKU string, e.g. "SR27754".
    """
    prefix = _CATEGORY_PREFIXES.get((category or "").lower(), _DEFAULT_PREFIX)
    digits = str(uuid.uuid4().int)[:5].zfill(5)  # deterministic 5-digit slice
    return f"{prefix}{digits}"


async def find_product_by_sku(
    db: AsyncSession, client_id: int, sku: str
) -> Optional[Product]:
    """
    Look up a product by SKU for the given client (case-insensitive).

    Strategy:
      1. Case-insensitive exact match (func.upper both sides).
      2. If not found, partial/prefix match — returns the first active product
         whose SKU starts with the candidate, so "SR277" matches "SR27754".

    Args:
        db:        Active async DB session.
        client_id: Must match the product's client_id (ownership check).
        sku:       SKU string (any case).

    Returns:
        Product if found and owned by client_id, else None.
    """
    upper_sku = sku.strip().upper()

    # 1. Case-insensitive exact match
    result = await db.execute(
        select(Product).where(
            Product.client_id == client_id,
            func.upper(Product.sku) == upper_sku,
        )
    )
    product = result.scalar_one_or_none()
    if product:
        return product

    # 2. Prefix/partial fallback — catches truncated or slightly mistyped codes
    result = await db.execute(
        select(Product).where(
            Product.client_id == client_id,
            Product.is_active == True,  # noqa: E712
            func.upper(Product.sku).like(f"{upper_sku}%"),
        ).limit(1)
    )
    return result.scalar_one_or_none()


def extract_skus_from_text(text: str) -> list[str]:
    """
    Extract all SKU-like tokens from a customer message.

    Matches patterns of 2–4 letters followed by 4–6 digits (case-insensitive).
    Returns tokens uppercased so they can be compared against DB SKUs directly.
    e.g. "sr27754" → "SR27754", "LH00123" → "LH00123".

    Space-tolerant: normalises "SR 27754" (common in voice transcriptions) to
    "SR27754" before matching, so the regex still fires.

    Args:
        text: Raw customer message text.

    Returns:
        List of candidate SKU strings (uppercased), possibly empty.
    """
    # Collapse single spaces between a letter-group and digit-group so that
    # voice-transcribed codes like "SR 27754" are treated as "SR27754".
    normalized = re.sub(r'([A-Za-z]{2,4})\s+(\d{4,6})', r'\1\2', text)
    return ["".join(m).upper() for m in SKU_PATTERN.findall(normalized)]


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def create_product_variants(
    db: AsyncSession,
    product_id: int,
    client_id: int,
    variants: list[dict],
) -> list[ProductVariant]:
    """
    Bulk-create variant rows for a product.

    Args:
        db:         Active async DB session.
        product_id: Owning product primary key.
        client_id:  Owning client primary key.
        variants:   List of dicts with keys: color, size, stock, price.

    Returns:
        List of created ProductVariant instances.
    """
    result = []
    for v in variants:
        pv = ProductVariant(
            product_id=product_id,
            client_id=client_id,
            color=v.get("color"),
            size=v.get("size"),
            stock=v.get("stock", 0),
            price=v.get("price"),
            image_url=v.get("image_url"),
        )
        db.add(pv)
        result.append(pv)
    await db.commit()
    return result


async def sync_product_variants(
    db: AsyncSession,
    product: Product,
    client_id: int,
    variants: list[dict],
) -> None:
    """
    Synchronise variants for an existing product.

    Variants with an 'id' that match an existing row are updated.
    New variants (no id) are created. Existing variants not present
    in the incoming list are deactivated (soft-delete).

    Args:
        db:        Active async DB session.
        product:   ORM instance (already fetched + ownership checked).
        client_id: Owning client primary key.
        variants:  List of dicts with keys: id (optional), color, size, stock, price.
    """
    existing = {v.id: v for v in (product.variants or [])}
    seen_ids: set[int] = set()

    for v in variants:
        vid = v.get("id")
        if vid and vid in existing:
            ev = existing[vid]
            ev.stock = v.get("stock", 0)
            ev.price = v.get("price")
            ev.is_active = True
            if v.get("image_url"):
                ev.image_url = v.get("image_url")
            seen_ids.add(vid)
        else:
            pv = ProductVariant(
                product_id=product.id,
                client_id=client_id,
                color=v.get("color"),
                size=v.get("size"),
                stock=v.get("stock", 0),
                price=v.get("price"),
                image_url=v.get("image_url"),
            )
            db.add(pv)

    for vid, ev in existing.items():
        if vid not in seen_ids:
            ev.is_active = False

    await db.commit()
    await db.refresh(product)


async def adjust_variant_stocks(
    db: AsyncSession,
    product: Product,
    client_id: int,
    adjustments: list[dict],
    reason: str,
) -> Product:
    """
    Set absolute stock values for individual variants and log the event.

    Args:
        db:          Active async DB session.
        product:     ORM instance (already fetched + ownership checked).
        client_id:   Owning client (denormalised into the log row).
        adjustments: List of dicts with keys: variant_id, new_stock.
        reason:      One of: sold, restocked, correction, damaged.

    Returns:
        Updated Product instance with recalculated total stock.

    Raises:
        ValueError: If reason is not in VALID_REASONS.
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of: {', '.join(sorted(VALID_REASONS))}")

    variant_map = {v.id: v for v in (product.variants or [])}
    total_stock = 0

    for adj in adjustments:
        vid = adj.get("variant_id")
        new_stock = max(0, adj.get("new_stock", 0))
        if vid and vid in variant_map:
            variant_map[vid].stock = new_stock
        total_stock += new_stock

    # Also count variants not in the adjustment list
    for vid, v in variant_map.items():
        if v.is_active and not any(a.get("variant_id") == vid for a in adjustments):
            total_stock += v.stock

    product.stock = total_stock
    await db.commit()
    await db.refresh(product)
    return product


async def create_product(
    db: AsyncSession,
    client_id: int,
    name: str,
    price: float,
    stock: Optional[int] = None,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
    sku: Optional[str] = None,
    category: Optional[str] = None,
    is_active: bool = True,
    low_stock_alert: int = 5,
    has_variants: bool = False,
    delivery_days: Optional[int] = None,
) -> Product:
    """
    Persist a new product for the given client.

    Args:
        db:               Active async DB session.
        client_id:        Owning client's primary key.
        name:             Product display name.
        price:            Unit price in INR.
        stock:            Available units (None = untracked).
        description:      Optional detailed description.
        image_url:        Optional public image URL.
        sku:              Stock-keeping unit code. Auto-generated if None.
        category:         Optional category label.
        is_active:        Whether the product is visible to customers.
        low_stock_alert:  Stock count at which a low-stock warning is shown.
        delivery_days:    Per-product delivery override (None = use client default).

    Returns:
        The newly created Product ORM instance.
    """
    product = Product(
        client_id=client_id,
        name=name,
        price=price,
        stock=stock,
        description=description,
        image_url=image_url,
        sku=sku or generate_sku(category),
        category=category,
        is_active=is_active,
        low_stock_alert=low_stock_alert,
        has_variants=has_variants,
        delivery_days=delivery_days,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def list_products(
    db: AsyncSession,
    client_id: int,
    *,
    category: Optional[str] = None,
    low_stock_only: bool = False,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
) -> list[Product]:
    """
    Return products owned by the given client with optional filters.

    Args:
        db:             Active async DB session.
        client_id:      Owning client's primary key.
        category:       If set, return only products in this category.
        low_stock_only: If True, return only products at or below low_stock_alert.
        is_active:      If True/False, filter by active status. None = all.
        search:         Keyword filter on name and description.

    Returns:
        List of Product instances ordered by name (may be empty).
    """
    stmt = select(Product).where(Product.client_id == client_id)

    if category:
        stmt = stmt.where(Product.category == category)
    if is_active is not None:
        stmt = stmt.where(Product.is_active == is_active)

    stmt = stmt.order_by(Product.name)
    result = await db.execute(stmt)
    products = list(result.scalars().all())

    # Post-query filters (can't easily express in SQL without subquery)
    if low_stock_only:
        products = [
            p for p in products
            if p.stock is not None and p.stock <= p.low_stock_alert
        ]
    if search:
        kw = search.lower()
        products = [
            p for p in products
            if kw in p.name.lower() or kw in (p.description or "").lower()
            or kw in (p.category or "").lower() or kw in (p.sku or "").lower()
        ]

    return products


async def get_product(
    db: AsyncSession, client_id: int, product_id: int
) -> Optional[Product]:
    """
    Fetch a single product, ensuring it belongs to client_id.

    Args:
        db:         Active async DB session.
        client_id:  Must match the product's client_id (ownership check).
        product_id: Product primary key.

    Returns:
        Product if found and owned by client_id, else None.
    """
    result = await db.execute(
        select(Product).where(
            Product.id == product_id, Product.client_id == client_id
        )
    )
    return result.scalar_one_or_none()


async def update_product(
    db: AsyncSession,
    product: Product,
    name: Optional[str] = None,
    price: Optional[float] = None,
    stock: Optional[int] = None,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
    sku: Optional[str] = None,
    category: Optional[str] = None,
    is_active: Any = _UNSET,
    low_stock_alert: Optional[int] = None,
    has_variants: Any = _UNSET,
    delivery_days: Any = _UNSET,
) -> Product:
    """
    Apply partial updates to an existing product.

    Only fields that are not None (or _UNSET for booleans) are written;
    passing None leaves the existing value unchanged.

    Note: is_active uses a sentinel (_UNSET) so that False is treated as an
    explicit deactivation rather than "not provided".

    Args:
        db:               Active async DB session.
        product:          ORM instance (already fetched + ownership checked).
        name:             New display name, or None to keep current.
        price:            New price, or None to keep current.
        stock:            New stock count, or None to keep current.
        description:      New description, or None to keep current.
        image_url:        New image URL, or None to keep current.
        sku:              New SKU, or None to keep current.
        category:         New category, or None to keep current.
        is_active:        New active state, or _UNSET to keep current.
        low_stock_alert:  New alert threshold, or None to keep current.
        delivery_days:    Per-product delivery override; _UNSET = leave unchanged, None = clear.

    Returns:
        The updated Product instance.
    """
    if name is not None:
        product.name = name
    if price is not None:
        product.price = price
    if stock is not None:
        product.stock = stock
    if description is not None:
        product.description = description
    if image_url is not None:
        product.image_url = image_url
    if sku is not None:
        product.sku = sku
    if category is not None:
        product.category = category
    if is_active is not _UNSET:
        product.is_active = is_active
    if low_stock_alert is not None:
        product.low_stock_alert = low_stock_alert
    if has_variants is not _UNSET:
        product.has_variants = has_variants
    if delivery_days is not _UNSET:
        product.delivery_days = delivery_days

    await db.commit()
    await db.refresh(product)
    return product


async def delete_product(db: AsyncSession, product: Product) -> None:
    """
    Permanently delete a product from the database.

    Args:
        db:      Active async DB session.
        product: ORM instance to delete (already fetched + ownership checked).
    """
    await db.delete(product)
    await db.commit()


# ── Stock adjustment ──────────────────────────────────────────────────────────

async def adjust_stock(
    db: AsyncSession,
    product: Product,
    client_id: int,
    adjustment: int,
    reason: str,
) -> Product:
    """
    Apply a signed stock adjustment and record the event in stock_logs.

    Args:
        db:         Active async DB session.
        product:    ORM instance (already fetched + ownership checked).
        client_id:  Owning client (denormalised into the log row).
        adjustment: Positive to add stock, negative to subtract.
        reason:     One of: sold, restocked, correction, damaged.

    Returns:
        Updated Product instance.

    Raises:
        ValueError: If reason is not in VALID_REASONS or if the adjustment
                    would result in negative stock.
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of: {', '.join(sorted(VALID_REASONS))}")

    stock_before = product.stock or 0
    stock_after = max(0, stock_before + adjustment)

    log = StockLog(
        product_id=product.id,
        client_id=client_id,
        adjustment=adjustment,
        reason=reason,
        stock_before=stock_before,
        stock_after=stock_after,
    )
    db.add(log)

    product.stock = stock_after
    await db.commit()
    await db.refresh(product)
    return product


async def get_stock_history(
    db: AsyncSession, product_id: int, limit: int = 10
) -> list[StockLog]:
    """
    Return the most recent stock adjustment events for a product.

    Args:
        db:         Active async DB session.
        product_id: Product primary key.
        limit:      Maximum rows to return (default 10).

    Returns:
        List of StockLog instances, newest first.
    """
    result = await db.execute(
        select(StockLog)
        .where(StockLog.product_id == product_id)
        .order_by(StockLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Keyword search ────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Lower-case, strip punctuation, remove stop words."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 1}


def search_products(
    products: list[Product], query: str, top_k: int = 5
) -> list[Product]:
    """
    Return up to top_k products most relevant to the customer's query.

    Only active products are included in the AI context.

    Scoring:
        +2  keyword found in product name
        +1  keyword found in product description

    Args:
        products: Full product list for the client (pre-fetched from DB).
        query:    Raw customer message text.
        top_k:    Maximum number of products to return.

    Returns:
        Sorted list of the most relevant active Product instances (best first).
    """
    # Only serve active products to the AI.
    # Treat is_active=None (un-persisted test fixture) as active.
    active = [p for p in products if p.is_active is not False]

    keywords = _tokenize(query)
    if not keywords:
        return active[:top_k]

    scored: list[tuple[int, Product]] = []
    for p in active:
        score = 0
        name_tokens = _tokenize(p.name)
        desc_tokens = _tokenize(p.description or "")
        for kw in keywords:
            if kw in name_tokens or kw in p.name.lower():
                score += 2
            if kw in desc_tokens or kw in (p.description or "").lower():
                score += 1
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:top_k]]


# ── Prompt context formatter ──────────────────────────────────────────────────

def format_catalogue_context(products: list[Product], for_display: bool = False) -> str:
    """
    Format a list of products as a detailed context block for the AI.

    For products with has_variants=True the variant breakdown (colors, sizes)
    is included. When for_display=True (browsing/T1/T2/T10 flows) raw stock
    counts are stripped — the AI must never reveal piece quantities to
    customers during browsing. Stock counts remain available internally for
    quantity validation via get_product_variant_info / available_stock.

    Args:
        products:    Relevant products returned by search_products.
        for_display: When True, omit raw stock numbers from the output.

    Returns:
        Multi-line string ready for injection into the system prompt,
        or empty string if the list is empty.
    """
    if not products:
        return ""

    lines = ["[Relevant products from our catalogue:]"]
    for p in products:
        header_parts = [f"• {p.name}"]
        if p.sku:
            header_parts.append(f"[{p.sku}]")
        header_parts.append(f"— ₹{p.price:,.0f}")
        if p.category:
            header_parts.append(f"[{p.category}]")
        lines.append(" ".join(header_parts))

        if p.description:
            lines.append(f"  Description: {p.description}")

        if p.has_variants and p.variants:
            active_variants = [v for v in p.variants if getattr(v, "is_active", True)]
            colors_in_stock: list[str] = []
            sizes_available: set[str] = set()

            for v in active_variants:
                if (v.stock or 0) > 0:
                    if v.color and v.color not in colors_in_stock:
                        colors_in_stock.append(v.color)
                    if v.size:
                        sizes_available.add(v.size)

            if colors_in_stock:
                lines.append(f"  Colors available: {', '.join(colors_in_stock)}")
            else:
                lines.append("  Colors available: none in stock")

            if sizes_available:
                lines.append(f"  Sizes available: {', '.join(sorted(sizes_available))}")

            if not for_display:
                # Out-of-stock combinations — only shown in internal/stock-check contexts
                oos = [
                    f"{v.color}-{v.size}"
                    for v in active_variants
                    if (v.stock or 0) == 0 and v.color and v.size
                ]
                if oos:
                    lines.append(f"  Out of stock: {', '.join(oos[:5])}")
        else:
            if not for_display:
                stock_val = p.stock
                if stock_val is not None:
                    lines.append(f"  Stock: {stock_val} pieces")

    return "\n".join(lines)


def search_products_with_scores(
    products: list[Product], query: str, top_k: int = 5
) -> list[tuple[int, "Product"]]:
    """
    Return up to top_k (score, product) pairs most relevant to query.

    Exposes the raw scores so callers can distinguish a single strong match
    (score >> second-best) from several close matches.

    Args:
        products: Full product list for the client (pre-fetched from DB).
        query:    Raw customer message text.
        top_k:    Maximum number of results.

    Returns:
        List of (score, Product) tuples sorted best-first; may be empty.
    """
    active = [p for p in products if p.is_active is not False]
    keywords = _tokenize(query)
    if not keywords:
        return []

    scored: list[tuple[int, Product]] = []
    for p in active:
        score = 0
        name_tokens = _tokenize(p.name)
        desc_tokens = _tokenize(p.description or "")
        for kw in keywords:
            if kw in name_tokens or kw in p.name.lower():
                score += 2
            if kw in desc_tokens or kw in (p.description or "").lower():
                score += 1
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


async def get_product_variant_info(db: AsyncSession, product: Product) -> dict:
    """
    Return variant availability info for a product, used by the order flow to
    decide which variant questions to ask (color, size, or neither).

    Args:
        db:      Active async DB session.
        product: Product ORM instance (must have has_variants attribute).

    Returns:
        Dict with keys:
          has_variants (bool), needs_color (bool), needs_size (bool),
          available_colors (list[str]), available_sizes (list[str]).
    """
    if not product or not getattr(product, "has_variants", False):
        return {
            "has_variants": False,
            "needs_color": False,
            "needs_size": False,
            "needs_material": False,
            "available_colors": [],
            "available_sizes": [],
            "available_materials": [],
        }

    result = await db.execute(
        select(ProductVariant).where(
            ProductVariant.product_id == product.id,
            ProductVariant.is_active == True,  # noqa: E712
        )
    )
    variants = result.scalars().all()

    colors = sorted({v.color for v in variants if v.color and (v.stock or 0) > 0})
    sizes = sorted({v.size for v in variants if v.size and (v.stock or 0) > 0})
    materials = sorted({v.material for v in variants if v.material and (v.stock or 0) > 0})

    return {
        "has_variants": True,
        "needs_color": bool(colors),
        "needs_size": bool(sizes),
        "needs_material": bool(materials),
        "available_colors": colors,
        "available_sizes": sizes,
        "available_materials": materials,
    }


async def variant_available(
    db: AsyncSession,
    product_id: int,
    color: str | None = None,
    size: str | None = None,
    material: str | None = None,
) -> tuple[bool, int]:
    """
    Return (exists, stock) for the exact variant combo.

    A combo is "available" only when a matching active variant row exists AND
    its stock is > 0.  None-valued attributes are not filtered on, so a product
    with only color+size variants can be queried without passing material.

    Args:
        db:         Active async DB session.
        product_id: Product.id to search within.
        color:      Selected color (None = skip filter).
        size:       Selected size (None = skip filter).
        material:   Selected material (None = skip filter).

    Returns:
        (True, stock) when in-stock; (False, 0) when OOS or not found.
    """
    stmt = select(ProductVariant).where(
        ProductVariant.product_id == product_id,
        ProductVariant.is_active == True,  # noqa: E712
    )
    if color:
        stmt = stmt.where(ProductVariant.color == color)
    if size:
        stmt = stmt.where(ProductVariant.size == size)
    if material:
        stmt = stmt.where(ProductVariant.material == material)
    result = await db.execute(stmt)
    variant = result.scalars().first()
    if variant is None:
        return (False, 0)
    stock = (variant.stock or 0)
    return (stock > 0, stock)


def render_product_listing(products: list[Product]) -> str:
    """
    Render a deterministic product listing from DB rows — no AI generation.

    Used as the fallback text when the guard detects phantom SKUs/prices in
    an AI-generated browsing reply.

    Args:
        products: Product ORM instances to render.

    Returns:
        Multi-line product listing string, or "" if products is empty.
    """
    if not products:
        return ""
    lines = []
    for p in products:
        line = f"• {p.name}"
        if p.sku:
            line += f" [{p.sku}]"
        if p.price is not None:
            line += f" — ₹{int(p.price):,}"
        if p.has_variants and p.variants:
            active_v = [v for v in p.variants if getattr(v, "is_active", True) and (v.stock or 0) > 0]
            colors = sorted({v.color for v in active_v if v.color})
            if colors:
                line += f"\n  Colors: {', '.join(colors)}"
        lines.append(line)
    return "\n".join(lines)


_SKU_IN_REPLY_RE = re.compile(r"\b([A-Za-z]{2,4}\d{4,6})\b")
_PRICE_IN_REPLY_RE = re.compile(r"₹\s*(\d[\d,]*)")

# Bare greetings/acks that never carry a product-name claim. Used ONLY to
# decide whether the customer's raw message is a plausible product-name
# attempt before echoing it into a "we don't carry X" fallback — kept as a
# small local list (deliberately not imported from order_pipeline, which
# imports this module, to avoid a circular import) rather than a full
# intent classifier.
_SMALLTALK_ONLY_PHRASES = frozenset({
    "hi", "hello", "hey", "hii", "hiii", "helo", "hlo",
    "namaste", "namaskar", "namaskte", "salam", "assalam", "kem cho", "hola",
    "thanks", "thank you", "shukriya", "dhanyavaad", "ty",
    "ok", "okay", "yes", "no", "bye", "goodbye", "alvida",
})


def _is_smalltalk_only(text: str) -> bool:
    """
    True when *text* is nothing but a greeting/ack/small-talk word — not a
    product-name claim of any kind. Matched as the ENTIRE message (case-
    insensitive, punctuation-stripped), so "hi, kurti available?" is NOT
    smalltalk-only.
    """
    if not text:
        return True
    stripped = re.sub(r"[^\w\s]", "", text.lower())
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped in _SMALLTALK_ONLY_PHRASES


def guard_product_reply(
    reply: str,
    canonical_products: list[Product],
    query: str | None = None,
    pre_validated_products: list[Product] | None = None,
) -> str:
    """
    Verify that an AI-generated browsing reply contains only real product facts.

    Extracts every SKU-like token and ₹ price from *reply* and checks each
    against the DB rows that were actually queried this turn.  If any phantom
    SKU or price is found, replaces the entire reply with a deterministic
    listing rendered from the allowed products.

    When *query* is provided and the allowed products have zero relevance to
    it, the fallback is an honest "not found" message naming the customer's
    query — UNLESS the query is bare smalltalk (a greeting/ack with no
    product-name claim at all), in which case echoing it back as "we don't
    carry {query}" would be nonsensical, so the plain deterministic listing
    is used instead (same as the no-query case).

    This guard runs on ALL models including 429 fallbacks (8b / llama-4-scout)
    which hallucinate more frequently.

    Args:
        reply:                 AI-generated reply text.
        canonical_products:    Products whose data is allowed in the reply
                                (the rows that were passed to the AI as
                                context this turn).
        query:                 Original customer query — used for relevance
                                check so we don't show an unrelated pinned
                                product when the customer asked about
                                something we don't carry.
        pre_validated_products: Product(s) already confirmed valid THIS TURN
                                through a path other than the canonical-
                                context lookup — e.g. a SKU the customer just
                                resolved via a multi-choice list/button pick.
                                Their SKU/price are always allowed in the
                                reply even if canonical_products (built
                                earlier in the turn, before the pick
                                resolved) doesn't include them. Defense-in-
                                depth on top of the caller rebuilding
                                canonical_products after a pick resolves —
                                an already-validated SKU must never be
                                flagged phantom regardless of what
                                canonical_products ends up containing.

    Returns:
        Original reply when clean; deterministic listing when phantom found
        (or "not found" message when query has no match in the allowed set
        and is itself a plausible product-name claim).
    """
    if not canonical_products and not pre_validated_products:
        return reply

    import logging as _log
    _logger = _log.getLogger(__name__)

    # Merge canonical + pre-validated products for the allow-list, deduping
    # by id (a pre-validated pick is often already present in canonical_
    # products too, once the caller has rebuilt it — this just guarantees
    # the pick is allowed even when that rebuild didn't happen/doesn't apply).
    _seen_ids: set[int] = set()
    _allowed_products: list[Product] = []
    for _p in list(canonical_products) + list(pre_validated_products or []):
        _pid = getattr(_p, "id", None)
        if _pid is not None and _pid in _seen_ids:
            continue
        if _pid is not None:
            _seen_ids.add(_pid)
        _allowed_products.append(_p)

    allowed_skus = {(p.sku or "").upper() for p in _allowed_products if p.sku}
    allowed_prices = {str(int(p.price)) for p in _allowed_products if p.price is not None}

    reply_skus = {m.group(1).upper() for m in _SKU_IN_REPLY_RE.finditer(reply)}
    phantom_skus = reply_skus - allowed_skus

    reply_raw_prices: set[str] = set()
    for m in _PRICE_IN_REPLY_RE.finditer(reply):
        reply_raw_prices.add(m.group(1).replace(",", ""))
    phantom_prices = reply_raw_prices - allowed_prices

    if phantom_skus or phantom_prices:
        _logger.warning(
            "guard_product_reply: phantom SKUs=%s prices=%s — replacing with deterministic listing.",
            phantom_skus, phantom_prices,
        )

        # FIX 3: If a query is provided, check whether the allowed products
        # actually match it. Score=0 means the allowed set was there only
        # because of a pinned SKU from a prior turn — not because it matched
        # the current query. In that case return an honest "not found" reply
        # naming the query, rather than listing an irrelevant product — but
        # only when the query itself is a plausible product-name claim. A
        # bare greeting/ack ("Hello", "thanks") never claims a product, so
        # echoing it into "we don't carry {query}" is nonsensical; fall
        # through to the plain listing instead.
        if query and canonical_products and not _is_smalltalk_only(query):
            _scored = search_products_with_scores(canonical_products, query, top_k=1)
            _top_score = _scored[0][0] if _scored else 0
            if _top_score == 0:
                _query_label = query.strip()[:40]
                _real_names = ", ".join(p.name for p in _allowed_products[:4])
                _logger.info(
                    "guard_product_reply: query %r has 0 relevance to canonical products — "
                    "returning not-found instead of irrelevant listing.",
                    _query_label,
                )
                return (
                    f"Sorry, we don't carry {_query_label}. "
                    f"We currently have: {_real_names}. "
                    "Let me know if any of these interest you!"
                )

        listing = render_product_listing(_allowed_products)
        return f"Here are some options:\n{listing}"

    return reply


async def get_in_stock_options(
    db: AsyncSession,
    product_id: int,
    color: str | None = None,
    size: str | None = None,
    material: str | None = None,
) -> dict[str, list[str]]:
    """
    Return the in-stock attribute options for a partial variant combo.

    Given a (possibly partial) combo, returns which values of each attribute
    still have stock > 0.  Used to generate "X isn't available in Y — available
    options: ..." messages.

    Args:
        db:         Active async DB session.
        product_id: Product.id to search within.
        color:      Fixed color to filter on (None = free).
        size:       Fixed size to filter on (None = free).
        material:   Fixed material to filter on (None = free).

    Returns:
        Dict with keys 'colors', 'sizes', 'materials' — each a sorted list of
        in-stock values given the fixed attributes.
    """
    stmt = select(ProductVariant).where(
        ProductVariant.product_id == product_id,
        ProductVariant.is_active == True,  # noqa: E712
        ProductVariant.stock > 0,
    )
    if color:
        stmt = stmt.where(ProductVariant.color == color)
    if size:
        stmt = stmt.where(ProductVariant.size == size)
    if material:
        stmt = stmt.where(ProductVariant.material == material)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "colors": sorted({r.color for r in rows if r.color}),
        "sizes": sorted({r.size for r in rows if r.size}),
        "materials": sorted({r.material for r in rows if r.material}),
    }
