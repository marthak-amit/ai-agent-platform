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
from app.services.language_templates import format_price

logger = logging.getLogger(__name__)


async def _get_order_count_for_year(_client_id: int, year: int, db: AsyncSession) -> int:
    """Return the total number of orders across ALL clients for the given year.

    Used to generate a globally unique order number (order_number has a unique
    constraint across all clients, so the sequence must be global too).
    """
    result = await db.execute(
        select(func.count()).where(
            func.extract("year", Order.created_at) == year,
        )
    )
    return result.scalar_one() or 0


async def mark_order_paid(
    db: AsyncSession,
    order: Order,
    client,
) -> bool:
    """
    Atomically transition an order to 'paid' and deduct stock — Phase 4 money lifecycle.

    Idempotent: returns False immediately if order.stock_deducted is already True,
    so duplicate 'paid' messages and webhook retries are harmless.

    Stock decrement happens HERE and ONLY here — never at order-creation time.
    The order's paid_at and payment_status are also set in this call.

    Args:
        db:    Async DB session.
        order: Order ORM instance to mark as paid.
        client: Client ORM instance (for owner notification).

    Returns:
        True if this call performed the transition; False if already done (idempotent skip).
    """
    if order.stock_deducted:
        logger.info(
            "mark_order_paid: order %s already paid — idempotent skip.",
            order.order_number,
        )
        return False

    try:
        order.status = "paid"
        order.payment_status = "paid"
        order.paid_at = datetime.now(timezone.utc)
        order.confirmed_at = order.confirmed_at or datetime.now(timezone.utc)
        order.stock_deducted = True
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("mark_order_paid commit failed for %s: %s", order.order_number, exc)
        raise

    # Stock deduction — best-effort after status commit so the order row is safe
    # even if the stock update fails (it will be retried via admin or background job).
    try:
        await _deduct_product_stock(db, order)
    except Exception as exc:
        logger.error(
            "mark_order_paid: stock deduction failed for %s: %s — order is paid but stock not decremented.",
            order.order_number, exc,
        )

    # Owner notification — best-effort
    try:
        await _notify_owner_new_order(order, client)
    except Exception as exc:
        logger.warning("mark_order_paid: owner notification failed for %s: %s", order.order_number, exc)

    # Invoice generation + send — best-effort, must never affect the paid
    # transition above (which has already committed).
    try:
        await _generate_and_send_invoice(db, order, client)
    except Exception as exc:
        logger.warning("mark_order_paid: invoice generation failed for %s: %s", order.order_number, exc)

    logger.info("mark_order_paid: order %s → paid, stock deducted.", order.order_number)
    return True


async def _generate_and_send_invoice(db: AsyncSession, order: Order, client) -> None:
    """
    Generate a branded PDF invoice for a paid/confirmed order, store it, and
    send it as a WhatsApp document to the customer (WhatsApp orders only —
    Instagram's customer_phone is an IGSID, not a real phone number).
    """
    from app.models.conversation import Conversation
    from app.services import invoice_service, whatsapp_service

    invoice_number = await invoice_service.generate_invoice_number(db, order.client_id)
    logo_bytes = await invoice_service.load_client_logo_bytes(client)
    pdf_bytes = invoice_service.generate_order_invoice(order, client, invoice_number, logo_bytes)
    invoice_url = invoice_service.save_order_invoice_pdf(order.id, pdf_bytes)

    order.invoice_number = invoice_number
    order.invoice_url = invoice_url
    await db.commit()

    channel = None
    if order.conversation_id:
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == order.conversation_id)
        )
        conv = conv_result.scalar_one_or_none()
        channel = conv.channel if conv else None

    if channel == "whatsapp":
        await whatsapp_service.send_document_message(
            to_phone_number=order.customer_phone,
            document_url=invoice_url,
            filename=f"Invoice-{invoice_number}.pdf",
            caption=f"🧾 Invoice for order {order.order_number}",
        )

    logger.info("Invoice %s generated for order %s.", invoice_number, order.order_number)


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
    mobile_number: Optional[str] = None,
    conversation_id: Optional[int] = None,
    product_sku: Optional[str] = None,
    variant_color: Optional[str] = None,
    variant_size: Optional[str] = None,
    variant_material: Optional[str] = None,
    product_id: Optional[int] = None,
    initial_status: str = "pending_payment",
    idempotency_key: Optional[str] = None,
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
        mobile_number: Optional delivery contact number (not identity).
        conversation_id: Optional linked conversation ID.
        product_sku: Optional product SKU.
        variant_color: Optional colour variant.
        variant_size: Optional size variant.
        variant_material: Optional material variant.
        product_id: Optional FK to products table.

    Returns:
        The newly created and refreshed Order instance.

    Raises:
        AssertionError: if quantity is not a positive integer — final guard
            so no order can be placed with qty < 1 regardless of any
            upstream slot-filling bug.
    """
    assert isinstance(quantity, int) and quantity >= 1, f"invalid order quantity: {quantity!r}"
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
        mobile_number=mobile_number,
        product_id=product_id,
        product_name=product_name,
        product_sku=product_sku,
        variant_color=variant_color,
        variant_size=variant_size,
        variant_material=variant_material,
        quantity=quantity,
        unit_price=unit_price,
        total_amount=unit_price * quantity,
        payment_method=payment_method,
        status=initial_status,
        idempotency_key=idempotency_key,
        # confirmed_at is set only when transitioning to paid (in mark_order_paid).
        # Orders start as pending_payment regardless of payment method.
    )

    db.add(order)
    await db.commit()
    await db.refresh(order)

    # Phase 4: stock is NEVER decremented at creation — only in mark_order_paid().
    # No backup deduction here.

    return order


async def _notify_owner_new_order(order: Order, client) -> None:
    """Send a WhatsApp summary of a new order to the business owner's registered phone."""
    from app.services import whatsapp_service

    if not client.phone:
        return

    _variant_parts = [
        p for p in [order.variant_color, order.variant_size, order.variant_material] if p
    ]
    _variant_line = f"Variant: {' / '.join(_variant_parts)}\n" if _variant_parts else ""
    message = (
        f"🛍️ New Order Received!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Order: #{order.order_number}\n"
        f"Product: {order.product_name} × {order.quantity}\n"
        f"{_variant_line}"
        f"Amount: {format_price(order.total_amount)}\n"
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

    if not order.product_id:
        return

    result = await db.execute(select(Product).where(Product.id == order.product_id))
    product = result.scalar_one_or_none()
    if not product:
        return

    qty = order.quantity

    _variant_material = getattr(order, "variant_material", None)
    if product.has_variants and (order.variant_color or order.variant_size or _variant_material):
        stmt = select(ProductVariant).where(ProductVariant.product_id == product.id)
        if order.variant_color:
            stmt = stmt.where(ProductVariant.color == order.variant_color)
        if order.variant_size:
            stmt = stmt.where(ProductVariant.size == order.variant_size)
        if _variant_material:
            stmt = stmt.where(ProductVariant.material == _variant_material)
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
        "Order #", "Customer", "Phone", "Product", "SKU", "Color", "Size", "Material",
        "Qty", "Unit Price", "Total", "Payment", "Payment Status",
        "Status", "Courier", "Tracking", "Created At", "Notes",
    ])
    for o in orders:
        writer.writerow([
            o.order_number, o.customer_name, o.customer_phone,
            o.product_name, o.product_sku or "", o.variant_color or "",
            o.variant_size or "", getattr(o, "variant_material", None) or "",
            o.quantity, o.unit_price, o.total_amount,
            o.payment_method, o.payment_status, o.status,
            o.courier_name or "", o.tracking_number or "",
            o.created_at.isoformat() if o.created_at else "",
            o.notes or "",
        ])
    return output.getvalue()
