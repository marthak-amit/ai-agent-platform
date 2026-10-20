"""
Per-tenant snapshot of the catalogue's STATIC rows (names, prices, categories, variant attributes, photos).

Why: agent v3 needs the whole catalogue on every message. For a real shop that is ~1,000 products and ~6,300 variants, and
loading them as ORM objects cost ~750 ms per turn. The static part changes rarely, so it is kept in process memory for
``TTL_SECONDS`` and dropped immediately by ``invalidate`` whenever the catalogue is written through ``catalogue_service``.
What changes with every order — stock and reservations — is NOT cached: ``agent_catalog.Catalogue.load`` re-reads those (three
narrow queries) on every turn, so availability is never stale.

Stale-while-revalidate: once a snapshot is older than ``TTL_SECONDS`` the next message is answered from it (up to ``STALE_SECONDS`` old)
while ONE background task reloads it, so a customer never waits ~1–2 s on a remote database for a reload (that was the "greeting
db=1703ms" turns). Any catalogue write still drops the snapshot (and its stale copy) at once, so an edit is never served stale.

Single-instance like the rate limiter and the message batcher; with several workers a write on one instance reaches the others
within ``TTL_SECONDS`` (move this to Redis then).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.product import Product
from app.models.product_variant import ProductVariant

logger = logging.getLogger(__name__)

TTL_SECONDS = 60.0
STALE_SECONDS = 3600.0          # a snapshot past its TTL may still answer a message (while a background reload runs) up to this age
_refreshing: set[int] = set()


@dataclass(frozen=True)
class VariantRow:
    """Static attributes of one active variant."""

    id: int
    color: str | None
    size: str | None
    material: str | None
    price: float | None
    image_url: str | None


@dataclass(frozen=True)
class ProductRow:
    """Static attributes of one active product and its variants."""

    id: int
    sku: str | None
    name: str
    price: float
    category: str | None
    description: str | None
    image_url: str | None
    delivery_days: int | None
    has_variants: bool
    variants: tuple[VariantRow, ...]


_snapshots: dict[int, tuple[float, tuple[ProductRow, ...]]] = {}
_names: dict[int, frozenset[str]] = {}


def invalidate(client_id: int | None = None) -> None:
    """Forget a tenant's snapshot (None = all). Called by every catalogue write."""
    if client_id is None:
        _snapshots.clear()
        _names.clear()
    else:
        _snapshots.pop(client_id, None)
        _names.pop(client_id, None)
    try:        # the per-tenant Catalogue object built from these rows (agent_catalog) goes with them
        from app.services import agent_catalog

        if client_id is None:
            agent_catalog._catalogue_cache.clear()
        else:
            agent_catalog._catalogue_cache.pop(client_id, None)
    except Exception:  # noqa: BLE001 - import cycle at startup / partial import: the identity check in Catalogue.load covers it
        pass


def get(client_id: int) -> tuple[ProductRow, ...] | None:
    """The cached rows if they are younger than ``TTL_SECONDS``."""
    hit = _snapshots.get(client_id)
    if hit is not None and time.monotonic() - hit[0] < TTL_SECONDS:
        return hit[1]
    return None


def get_stale(client_id: int) -> tuple[ProductRow, ...] | None:
    """The cached rows even past ``TTL_SECONDS`` (but younger than ``STALE_SECONDS``) — to answer now while ``refresh_soon`` reloads them."""
    hit = _snapshots.get(client_id)
    if hit is not None and time.monotonic() - hit[0] < STALE_SECONDS:
        return hit[1]
    return None


def refresh_soon(client_id: int, db) -> None:
    """Reload a tenant's snapshot in a background task on its own session (at most one reload per tenant at a time)."""
    if client_id in _refreshing:
        return
    _refreshing.add(client_id)

    async def _run() -> None:
        """Load the rows with a fresh session bound to the same engine as the caller's."""
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(db.bind, expire_on_commit=False) as session:
                await load_rows(session, client_id)
        except Exception as exc:  # noqa: BLE001 - the stale rows keep answering; the next message retries
            logger.warning("catalogue snapshot reload failed client=%s: %s", client_id, exc)
        finally:
            _refreshing.discard(client_id)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        _refreshing.discard(client_id)


def put(client_id: int, rows: tuple[ProductRow, ...]) -> None:
    """Store a freshly loaded snapshot."""
    _snapshots[client_id] = (time.monotonic(), rows)
    _names.pop(client_id, None)
    try:        # keep the perceptual-hash index in step with the catalogue photos (a no-op unless a photo changed; builds off the loop)
        from app.services import image_fingerprint

        image_fingerprint.ensure_index(client_id, [(r.sku, r.image_url) for r in rows if r.sku and r.image_url])
    except Exception as exc:  # noqa: BLE001 - the index is an optimisation
        logger.debug("image index schedule failed client=%s: %s", client_id, exc)


def normalize_name(text: str) -> str:
    """Comparison form of a product name / message ("A-Line  Cotton Kurti" → "a line cotton kurti")."""
    from app.services import indic_vocab

    return " ".join(indic_vocab.tokenize(text))


def product_names(client_id: int) -> frozenset[str] | None:
    """Normalised names of the tenant's products when a snapshot is cached (no database access), else None."""
    rows = get(client_id)
    if rows is None:
        return None
    if client_id not in _names:
        _names[client_id] = frozenset(normalize_name(r.name) for r in rows)
    return _names[client_id]


async def load_rows(db, client_id: int) -> tuple[ProductRow, ...]:
    """Read the static rows (two narrow queries, no ORM entities) and cache them."""
    products = (await db.execute(
        select(
            Product.id, Product.sku, Product.name, Product.price, Product.category, Product.description, Product.image_url,
            Product.delivery_days, Product.has_variants,
        ).where(Product.client_id == client_id, Product.is_active.is_(True)).order_by(Product.name)
    )).all()
    by_product: dict[int, list[VariantRow]] = {}
    for vid, pid, color, size, material, price, image_url, enhanced_url, enhanced_status, enhanced_approved in (await db.execute(
        select(
            ProductVariant.id, ProductVariant.product_id, ProductVariant.color, ProductVariant.size, ProductVariant.material,
            ProductVariant.price, ProductVariant.image_url, ProductVariant.enhanced_image_url, ProductVariant.enhanced_status,
            ProductVariant.enhanced_approved,
        ).where(ProductVariant.client_id == client_id, ProductVariant.is_active.is_(True))
    )).all():
        shown = enhanced_url if (enhanced_status == "done" and enhanced_approved and enhanced_url) else image_url
        by_product.setdefault(pid, []).append(VariantRow(vid, color, size, material, price, shown))
    rows = tuple(
        ProductRow(pid, sku, name, float(price or 0), category, description, image_url, delivery_days, bool(has_variants),
                   tuple(by_product.get(pid, ())))
        for pid, sku, name, price, category, description, image_url, delivery_days, has_variants in products
    )
    put(client_id, rows)
    return rows


async def load_stock(db, client_id: int) -> tuple[dict[int, int], dict[int, int | None]]:
    """
    Current stock: ({variant_id: stock}, {product_id: stock or None for an untracked product}) — never cached.

    Each map comes back as ONE jsonb value built by Postgres (``jsonb_object_agg``) instead of thousands of result rows: a 1,000-product /
    6,000-variant shop used to materialise ~7,000 ``Row`` objects per customer message on the event loop (~25 ms, and a GC pause now and
    then); decoding one JSON document is a few milliseconds.
    """
    variant_json = (await db.execute(
        select(func.coalesce(func.jsonb_object_agg(cast(ProductVariant.id, String), func.coalesce(ProductVariant.stock, 0)), cast("{}", JSONB)))
        .where(ProductVariant.client_id == client_id, ProductVariant.is_active.is_(True))
    )).scalar_one()
    product_json = (await db.execute(
        select(func.coalesce(func.jsonb_object_agg(cast(Product.id, String), Product.stock), cast("{}", JSONB)))
        .where(Product.client_id == client_id, Product.is_active.is_(True))
    )).scalar_one()
    variants = {int(vid): int(stock or 0) for vid, stock in (variant_json or {}).items()}
    products = {int(pid): (None if stock is None else int(stock)) for pid, stock in (product_json or {}).items()}
    return variants, products
