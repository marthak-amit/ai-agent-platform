"""
Order management service.

Handles order creation, status updates, stats, and customer/owner notifications.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order

logger = logging.getLogger(__name__)


async def _get_order_count_for_year(client_id: int, year: int, db: AsyncSession) -> int:
    """Return how many orders exist for this client in the given year."""
    result = await db.execute(
        select(func.count()).where(
            Order.client_id == client_id,
            func.extract("year", Order.created_at) == year,
        )
    )
    return result.scalar_one() or 0


async def create_order(
    db: AsyncSession,
    client_id: int,
    customer_name: str,
    customer_phone: str,
    delivery_address: str,
    product_name: str,
    quantity: int,
    unit_price: float,
    payment_method: str = "COD",
    conversation_id: Optional[int] = None,
    product_sku: Optional[str] = None,
    variant_color: Optional[str] = None,
    variant_size: Optional[str] = None,
    product_id: Optional[int] = None,
) -> Order:
    """
    Create and persist a new order, then notify the business owner via WhatsApp.

    Args:
        db: Async DB session.
        client_id: ID of the client (business) that owns this order.
        customer_name: Customer's full name.
        customer_phone: Customer's phone in E.164 format without '+'.
        delivery_address: Full delivery address.
        product_name: Product name as a string (denormalised for display).
        quantity: Number of units ordered.
        unit_price: Price per unit in INR.
        payment_method: 'COD' or 'UPI'.
        conversation_id: Optional linked conversation ID.
        product_sku: Optional product SKU.
        variant_color: Optional colour variant.
        variant_size: Optional size variant.
        product_id: Optional FK to products table.

    Returns:
        The newly created and refreshed Order instance.
    """
    year = datetime.now(timezone.utc).year
    count = await _get_order_count_for_year(client_id, year, db)
    order_number = f"ORD-{year}-{str(count + 1).zfill(4)}"

    order = Order(
        order_number=order_number,
        client_id=client_id,
        conversation_id=conversation_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        delivery_address=delivery_address,
        product_id=product_id,
        product_name=product_name,
        product_sku=product_sku,
        variant_color=variant_color,
        variant_size=variant_size,
        quantity=quantity,
        unit_price=unit_price,
        total_amount=unit_price * quantity,
        payment_method=payment_method,
        status="confirmed",
        confirmed_at=datetime.now(timezone.utc),
    )

    db.add(order)
    await db.commit()
    await db.refresh(order)

    # Safety backup: deduct stock here in case the webhook path skipped it.
    # The stock_deducted flag ensures we never deduct twice.
    if product_id and not order.stock_deducted:
        try:
            await _deduct_product_stock(db, order)
        except Exception as exc:
            logger.warning("Backup stock deduction failed for order %s: %s", order.order_number, exc)

    # Notify owner — best-effort; failure must not break the order flow
    try:
        from app.models.client import Client
        result = await db.execute(select(Client).where(Client.id == client_id))
        client = result.scalar_one_or_none()
        if client:
            await _notify_owner_new_order(order, client)
    except Exception as exc:
        logger.warning("Owner notification failed for order %s: %s", order.order_number, exc)

    return order


async def _notify_owner_new_order(order: Order, client) -> None:
    """Send a WhatsApp summary of a new order to the business owner's registered phone."""
    from app.services import whatsapp_service

    if not client.phone:
        return

    message = (
        f"🛍️ New Order Received!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Order: #{order.order_number}\n"
        f"Product: {order.product_name} × {order.quantity}\n"
        f"Amount: ₹{order.total_amount:.0f}\n"
        f"Customer: {order.customer_name}\n"
        f"Phone: {order.customer_phone}\n"
        f"Address: {order.delivery_address}\n"
        f"Payment: {order.payment_method}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"View in dashboard: /orders"
    )

    await whatsapp_service.send_text_message(client.phone, message)


async def get_order(order_id: int, client_id: int, db: AsyncSession) -> Order:
    """
    Fetch a single order by ID, scoped to the given client.

    Raises:
        ValueError: If the order does not exist or belongs to a different client.
    """
    result = await db.execute(
        select(Order).where(Order.id == order_id, Order.client_id == client_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise ValueError(f"Order {order_id} not found")
    return order


async def update_order_status(
    db: AsyncSession,
    order_id: int,
    client_id: int,
    new_status: str,
    tracking_number: Optional[str] = None,
    courier_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> Order:
    """
    Update an order's status and set the appropriate timestamp field.

    When status is 'dispatched', also notifies the customer via WhatsApp.

    Args:
        db: Async DB session.
        order_id: ID of the order to update.
        client_id: Owning client ID (used for access check).
        new_status: Target status string.
        tracking_number: Courier tracking number (for dispatched status).
        courier_name: Courier company name (for dispatched status).
        notes: Optional notes to append.

    Returns:
        Updated Order instance.
    """
    order = await get_order(order_id, client_id, db)
    order.status = new_status

    now = datetime.now(timezone.utc)
    if new_status == "paid":
        order.paid_at = now
        order.payment_status = "paid"
    elif new_status == "dispatched":
        order.dispatched_at = now
        if tracking_number:
            order.tracking_number = tracking_number
        if courier_name:
            order.courier_name = courier_name
        # Notify customer — best-effort
        try:
            from app.models.client import Client
            result = await db.execute(select(Client).where(Client.id == client_id))
            client = result.scalar_one_or_none()
            if client:
                await _notify_customer_dispatched(order, client)
        except Exception as exc:
            logger.warning("Customer dispatch notification failed: %s", exc)
    elif new_status == "delivered":
        order.delivered_at = now

    if notes:
        order.notes = notes

    await db.commit()
    await db.refresh(order)
    return order


async def _notify_customer_dispatched(order: Order, client) -> None:
    """Send a WhatsApp dispatch notification to the customer."""
    from app.services import whatsapp_service

    if not client.whatsapp_phone_number_id:
        return

    message = (
        f"📦 Your order is on the way!\n\n"
        f"Order #{order.order_number}\n"
        f"{order.product_name} × {order.quantity}"
    )
    if order.tracking_number:
        message += f"\nTracking: {order.tracking_number}"
        if order.courier_name:
            message += f" ({order.courier_name})"
    message += "\nExpected delivery: 3–5 days"

    await whatsapp_service.send_text_message(order.customer_phone, message)


async def get_orders(
    db: AsyncSession,
    client_id: int,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    skip: int = 0,
    limit: int = 50,
) -> list[Order]:
    """
    Return a paginated, optionally filtered list of orders for a client.

    Args:
        db: Async DB session.
        client_id: Owning client ID.
        status: Optional status filter.
        search: Optional search string matched against order_number and customer_name.
        date_from: Optional start date filter (inclusive).
        date_to: Optional end date filter (inclusive).
        skip: Pagination offset.
        limit: Max rows to return.

    Returns:
        List of Order instances ordered by created_at descending.
    """
    from sqlalchemy import or_

    q = select(Order).where(Order.client_id == client_id)
    if status:
        q = q.where(Order.status == status)
    if search:
        pattern = f"%{search}%"
        q = q.where(
            or_(
                Order.order_number.ilike(pattern),
                Order.customer_name.ilike(pattern),
                Order.customer_phone.ilike(pattern),
            )
        )
    if date_from:
        q = q.where(func.date(Order.created_at) >= date_from)
    if date_to:
        q = q.where(func.date(Order.created_at) <= date_to)

    q = q.order_by(Order.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_orders_stats(client_id: int, db: AsyncSession) -> dict:
    """
    Return aggregate order stats for a client's dashboard.

    Returns:
        Dict with today_orders, today_revenue, pending_dispatch, cod_pending,
        total_orders, total_revenue.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async def _count(where_clauses) -> int:
        r = await db.execute(select(func.count()).where(*where_clauses))
        return r.scalar_one() or 0

    async def _sum(where_clauses) -> float:
        r = await db.execute(select(func.sum(Order.total_amount)).where(*where_clauses))
        return float(r.scalar_one() or 0)

    today_orders = await _count([Order.client_id == client_id, Order.created_at >= today_start])
    today_revenue = await _sum([Order.client_id == client_id, Order.created_at >= today_start])
    pending_dispatch = await _count([Order.client_id == client_id, Order.status == "confirmed"])
    cod_pending = await _count([
        Order.client_id == client_id,
        Order.payment_method == "COD",
        Order.payment_status == "pending",
        Order.status.notin_(["cancelled"]),
    ])
    total_orders = await _count([Order.client_id == client_id])
    total_revenue = await _sum([Order.client_id == client_id])

    return {
        "today_orders": today_orders,
        "today_revenue": today_revenue,
        "pending_dispatch": pending_dispatch,
        "cod_pending": cod_pending,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
    }


async def _deduct_product_stock(db: AsyncSession, order: Order) -> None:
    """
    Backup stock deduction called from create_order when product_id is set.

    Only runs if order.stock_deducted is False, so it is safe to call even when
    the webhook already handled deduction. Commits once and sets stock_deducted.
    """
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    if order.stock_deducted or not order.product_id:
        return

    result = await db.execute(select(Product).where(Product.id == order.product_id))
    product = result.scalar_one_or_none()
    if not product:
        return

    qty = order.quantity

    if product.has_variants and (order.variant_color or order.variant_size):
        stmt = select(ProductVariant).where(ProductVariant.product_id == product.id)
        if order.variant_color:
            stmt = stmt.where(ProductVariant.color == order.variant_color)
        if order.variant_size:
            stmt = stmt.where(ProductVariant.size == order.variant_size)
        vresult = await db.execute(stmt)
        variant = vresult.scalar_one_or_none()
        if variant:
            variant.stock = max(0, variant.stock - qty)
            all_result = await db.execute(
                select(ProductVariant).where(ProductVariant.product_id == product.id)
            )
            product.stock = sum(v.stock for v in all_result.scalars().all())
    else:
        product.stock = max(0, (product.stock or 0) - qty)

    order.stock_deducted = True
    await db.commit()


def orders_to_csv(orders: list[Order]) -> str:
    """
    Serialise a list of orders to a CSV string.

    Args:
        orders: List of Order ORM instances.

    Returns:
        UTF-8 CSV string with header row.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order #", "Customer", "Phone", "Product", "SKU", "Color", "Size",
        "Qty", "Unit Price", "Total", "Payment", "Payment Status",
        "Status", "Courier", "Tracking", "Created At", "Notes",
    ])
    for o in orders:
        writer.writerow([
            o.order_number, o.customer_name, o.customer_phone,
            o.product_name, o.product_sku or "", o.variant_color or "",
            o.variant_size or "", o.quantity, o.unit_price, o.total_amount,
            o.payment_method, o.payment_status, o.status,
            o.courier_name or "", o.tracking_number or "",
            o.created_at.isoformat() if o.created_at else "",
            o.notes or "",
        ])
    return output.getvalue()
