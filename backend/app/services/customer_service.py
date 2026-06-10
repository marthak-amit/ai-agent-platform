"""
Customer profile service.

Handles creation, upsert, and stat updates for the customers table.
One Customer row per (client_id, phone) pair — created automatically on the
first incoming message and kept in sync on every order confirmation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer

logger = logging.getLogger(__name__)


async def get_customer(
    db: AsyncSession,
    client_id: int,
    phone: str,
) -> Optional[Customer]:
    """Return the Customer for this (client_id, phone) pair, or None."""
    result = await db.execute(
        select(Customer).where(
            Customer.client_id == client_id,
            Customer.phone == phone,
        )
    )
    return result.scalar_one_or_none()


async def upsert_customer(
    db: AsyncSession,
    client_id: int,
    phone: str,
    name: Optional[str] = None,
    address: Optional[str] = None,
    preferred_language: Optional[str] = None,
    preferred_payment: Optional[str] = None,
) -> Customer:
    """
    Get-or-create a Customer row and update last_message_at.

    Only fills in name/address/preferred_language/preferred_payment if the
    existing value is None — preserves any data already set.

    Args:
        db:                 Async DB session.
        client_id:          Owning client ID.
        phone:              Customer's phone number (E.164 without '+').
        name:               Customer name, if known from this message.
        address:            Delivery address, if known.
        preferred_language: Detected language for this message.
        preferred_payment:  Payment method detected ("COD" / "UPI").

    Returns:
        The up-to-date Customer instance (not yet committed).
    """
    customer = await get_customer(db, client_id, phone)
    now = datetime.now(timezone.utc)

    if customer is None:
        customer = Customer(
            client_id=client_id,
            phone=phone,
            name=name,
            address=address,
            preferred_language=preferred_language or "english",
            preferred_payment=preferred_payment,
            first_message_at=now,
            last_message_at=now,
        )
        db.add(customer)
        await db.flush()
        logger.info("Created new customer profile phone=%s client=%s", phone, client_id)
    else:
        customer.last_message_at = now
        if name and not customer.name:
            customer.name = name
        if address and not customer.address:
            customer.address = address
        if preferred_language:
            customer.preferred_language = preferred_language
        if preferred_payment and not customer.preferred_payment:
            customer.preferred_payment = preferred_payment

    return customer


async def record_order(
    db: AsyncSession,
    client_id: int,
    phone: str,
    order_total: float,
    customer_name: Optional[str] = None,
    delivery_address: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> Customer:
    """
    Increment order stats on the customer profile when an order is confirmed.

    Creates the customer row if it doesn't exist yet (edge-case guard).

    Args:
        db:               Async DB session.
        client_id:        Owning client ID.
        phone:            Customer phone number.
        order_total:      Total amount for this order.
        customer_name:    Name from the order (fills in if missing on profile).
        delivery_address: Address from the order (fills in if missing).
        payment_method:   Payment method used ("COD" / "UPI").

    Returns:
        Updated Customer instance (committed by caller).
    """
    customer = await upsert_customer(
        db,
        client_id=client_id,
        phone=phone,
        name=customer_name,
        address=delivery_address,
        preferred_payment=payment_method,
    )
    customer.total_orders += 1
    customer.total_spent += order_total
    customer.last_order_at = datetime.now(timezone.utc)
    return customer


async def set_vip(db: AsyncSession, customer_id: int, client_id: int, is_vip: bool) -> Customer:
    """Toggle VIP status for a customer."""
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client_id,
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise ValueError(f"Customer {customer_id} not found")
    customer.is_vip = is_vip
    if is_vip and customer.tags:
        if "vip" not in customer.tags.split(","):
            customer.tags = customer.tags + ",vip"
    elif is_vip:
        customer.tags = "vip"
    await db.commit()
    await db.refresh(customer)
    return customer


async def set_blocked(
    db: AsyncSession, customer_id: int, client_id: int, is_blocked: bool
) -> Customer:
    """Block or unblock a customer."""
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client_id,
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise ValueError(f"Customer {customer_id} not found")
    customer.is_blocked = is_blocked
    await db.commit()
    await db.refresh(customer)
    return customer


async def update_customer(
    db: AsyncSession,
    customer_id: int,
    client_id: int,
    name: Optional[str] = None,
    email: Optional[str] = None,
    address: Optional[str] = None,
    notes: Optional[str] = None,
    tags: Optional[str] = None,
    preferred_language: Optional[str] = None,
    preferred_payment: Optional[str] = None,
) -> Customer:
    """Apply a partial update to a customer's editable fields."""
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client_id,
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise ValueError(f"Customer {customer_id} not found")

    if name is not None:
        customer.name = name
    if email is not None:
        customer.email = email
    if address is not None:
        customer.address = address
    if notes is not None:
        customer.notes = notes
    if tags is not None:
        customer.tags = tags
    if preferred_language is not None:
        customer.preferred_language = preferred_language
    if preferred_payment is not None:
        customer.preferred_payment = preferred_payment

    await db.commit()
    await db.refresh(customer)
    return customer


async def list_customers(
    db: AsyncSession,
    client_id: int,
    search: Optional[str] = None,
    filter_vip: bool = False,
    filter_new: bool = False,
    filter_inactive: bool = False,
    sort_by: str = "latest",
    skip: int = 0,
    limit: int = 50,
) -> list[Customer]:
    """
    Return a filtered, sorted, paginated list of customers for a client.

    Args:
        db:              Async DB session.
        client_id:       Owning client ID.
        search:          Optional search string matched against name and phone.
        filter_vip:      If True, only VIP customers.
        filter_new:      If True, only customers with zero orders.
        filter_inactive: If True, only customers with no activity in 30+ days.
        sort_by:         "latest" | "most_orders" | "highest_spent"
        skip:            Pagination offset.
        limit:           Max rows to return.

    Returns:
        List of Customer instances.
    """
    from datetime import timedelta
    from sqlalchemy import or_, desc

    q = select(Customer).where(
        Customer.client_id == client_id,
        Customer.is_blocked == False,  # noqa: E712
    )

    if search:
        pattern = f"%{search}%"
        q = q.where(
            or_(
                Customer.name.ilike(pattern),
                Customer.phone.ilike(pattern),
            )
        )
    if filter_vip:
        q = q.where(Customer.is_vip == True)  # noqa: E712
    if filter_new:
        q = q.where(Customer.total_orders == 0)
    if filter_inactive:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        q = q.where(Customer.last_message_at < cutoff)

    if sort_by == "most_orders":
        q = q.order_by(desc(Customer.total_orders))
    elif sort_by == "highest_spent":
        q = q.order_by(desc(Customer.total_spent))
    else:
        q = q.order_by(desc(Customer.last_message_at))

    q = q.offset(skip).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_customer_stats(db: AsyncSession, client_id: int) -> dict:
    """
    Return aggregate customer stats for the dashboard.

    Returns:
        Dict with total_customers, active_this_month, vip_customers,
        avg_order_value.
    """
    from datetime import timedelta
    from sqlalchemy import func

    async def _count(where_clauses) -> int:
        r = await db.execute(select(func.count()).where(*where_clauses))
        return r.scalar_one() or 0

    async def _scalar(stmt):
        r = await db.execute(stmt)
        return r.scalar_one() or 0.0

    month_ago = datetime.now(timezone.utc) - timedelta(days=30)

    total = await _count([Customer.client_id == client_id])
    active = await _count([
        Customer.client_id == client_id,
        Customer.last_message_at >= month_ago,
    ])
    vip = await _count([Customer.client_id == client_id, Customer.is_vip == True])  # noqa: E712

    avg_stmt = select(func.avg(Customer.total_spent)).where(
        Customer.client_id == client_id,
        Customer.total_orders > 0,
    )
    avg_val = await _scalar(avg_stmt)

    return {
        "total_customers": total,
        "active_this_month": active,
        "vip_customers": vip,
        "avg_order_value": round(float(avg_val), 2),
    }
