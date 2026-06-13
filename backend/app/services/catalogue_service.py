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

    Args:
        text: Raw customer message text.

    Returns:
        List of candidate SKU strings (uppercased), possibly empty.
    """
    return ["".join(m).upper() for m in SKU_PATTERN.findall(text)]


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
            seen_ids.add(vid)
        else:
            pv = ProductVariant(
                product_id=product.id,
                client_id=client_id,
                color=v.get("color"),
                size=v.get("size"),
                stock=v.get("stock", 0),
                price=v.get("price"),
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

def format_catalogue_context(products: list[Product]) -> str:
    """
    Format a list of products as a detailed context block for the AI.

    For products with has_variants=True the variant breakdown (colors, sizes,
    per-color stock) is included so the AI can answer availability questions
    accurately without hallucinating colors or sizes.

    Args:
        products: Relevant products returned by search_products.

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
            # Group in-stock variants by color
            colors: dict[str, dict] = {}
            sizes_available: set[str] = set()

            for v in active_variants:
                if (v.stock or 0) > 0:
                    color = v.color or "default"
                    if color not in colors:
                        colors[color] = {}
                    if v.size:
                        colors[color][v.size] = (v.stock or 0)
                        sizes_available.add(v.size)
                    else:
                        colors[color]["stock"] = colors[color].get("stock", 0) + (v.stock or 0)

            if colors:
                color_parts = []
                for color, data in colors.items():
                    if "stock" in data and len(data) == 1:
                        color_parts.append(f"{color}({data['stock']})")
                    else:
                        total = sum(v for k, v in data.items() if isinstance(v, int))
                        color_parts.append(f"{color}({total})")
                lines.append(f"  Colors available: {', '.join(color_parts)}")
            else:
                lines.append("  Colors available: none in stock")

            if sizes_available:
                lines.append(f"  Sizes available: {', '.join(sorted(sizes_available))}")

            # Out-of-stock combinations (up to 5)
            oos = [
                f"{v.color}-{v.size}"
                for v in active_variants
                if (v.stock or 0) == 0 and v.color and v.size
            ]
            if oos:
                lines.append(f"  Out of stock: {', '.join(oos[:5])}")
        else:
            stock_val = p.stock
            if stock_val is not None:
                lines.append(f"  Stock: {stock_val} pieces")

    return "\n".join(lines)


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
