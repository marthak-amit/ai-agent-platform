"""
Stock reservation lifecycle for orders awaiting manual UPI verification.

    pending_payment  → reserve   (StockReservation rows, status 'active')
    paid             → consume   (rows 'consumed'; real stock deducted by
                                  order_service.apply_stock_deduction in the
                                  SAME transaction)
    cancelled        → release   (rows 'released'; nothing was deducted)

Available stock for a NEW order = stock − Σ active reservations. All functions
here only flush — the caller owns the transaction so approve/cancel stay atomic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.product_variant import ProductVariant
from app.models.stock_reservation import (
    RESERVATION_ACTIVE,
    RESERVATION_CONSUMED,
    RESERVATION_RELEASED,
    StockReservation,
)

logger = logging.getLogger(__name__)


async def _find_variant_id(
    db: AsyncSession, product_id: int, color: str | None, size: str | None, material: str | None
) -> int | None:
    """Resolve the ProductVariant id for a color/size/material selection, or None."""
    if not (color or size or material):
        return None
    stmt = select(ProductVariant.id).where(ProductVariant.product_id == product_id)
    if color:
        stmt = stmt.where(ProductVariant.color == color)
    if size:
        stmt = stmt.where(ProductVariant.size == size)
    if material:
        stmt = stmt.where(ProductVariant.material == material)
    return (await db.execute(stmt)).scalars().first()


def _order_lines(order: Order) -> list[tuple[int | None, str | None, str | None, str | None, int]]:
    """(product_id, color, size, material, qty) per line; flat columns for legacy orders."""
    items = getattr(order, "line_items", None) or []
    if items:
        return [
            (li.product_id, li.variant_color, li.variant_size, li.variant_material, li.quantity)
            for li in items
        ]
    return [
        (order.product_id, order.variant_color, order.variant_size,
         getattr(order, "variant_material", None), order.quantity)
    ]


async def reserve_lines(
    db: AsyncSession,
    order_id: int,
    lines: list[tuple[int | None, str | None, str | None, str | None, int]],
) -> int:
    """
    Create active reservations for (product_id, color, size, material, qty) lines.

    Idempotent per order. Takes plain tuples (not the Order) so order creation
    can reserve in the SAME transaction as the order insert without touching
    an unloaded relationship in async context.

    Returns:
        Number of reservation rows created (0 when already reserved).
    """
    existing = (
        await db.execute(
            select(func.count()).select_from(StockReservation).where(StockReservation.order_id == order_id)
        )
    ).scalar_one()
    if existing:
        return 0
    created = 0
    for product_id, color, size, material, qty in lines:
        if not product_id:
            continue
        variant_id = await _find_variant_id(db, product_id, color, size, material)
        db.add(StockReservation(
            order_id=order_id, product_id=product_id, variant_id=variant_id,
            quantity=qty, status=RESERVATION_ACTIVE,
        ))
        created += 1
    await db.flush()
    return created


async def reserve_for_order(db: AsyncSession, order: Order) -> int:
    """Reserve stock for an already-loaded order (line_items must be loaded)."""
    return await reserve_lines(db, order.id, _order_lines(order))


async def _resolve(db: AsyncSession, order: Order, new_status: str) -> None:
    """Move every ACTIVE reservation of `order` to `new_status`."""
    await db.execute(
        update(StockReservation)
        .where(StockReservation.order_id == order.id, StockReservation.status == RESERVATION_ACTIVE)
        .values(status=new_status, resolved_at=datetime.now(timezone.utc))
    )


async def release_for_order(db: AsyncSession, order: Order) -> None:
    """Release an order's active reservations (cancel/expiry). No stock was deducted."""
    await _resolve(db, order, RESERVATION_RELEASED)


async def consume_for_order(db: AsyncSession, order: Order) -> None:
    """Mark an order's active reservations consumed (paid — stock is deducted alongside)."""
    await _resolve(db, order, RESERVATION_CONSUMED)


async def reserved_quantity(
    db: AsyncSession, product_id: int, variant_id: int | None = None
) -> int:
    """
    Units currently held by active reservations for a product (or one variant).

    Used by order creation so two customers can't both reserve the last piece.
    """
    stmt = select(func.coalesce(func.sum(StockReservation.quantity), 0)).where(
        StockReservation.product_id == product_id,
        StockReservation.status == RESERVATION_ACTIVE,
    )
    if variant_id is not None:
        stmt = stmt.where(StockReservation.variant_id == variant_id)
    return int((await db.execute(stmt)).scalar_one() or 0)
