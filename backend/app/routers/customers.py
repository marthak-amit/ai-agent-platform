"""
Customers API router (protected — requires JWT).

Provides the React dashboard with customer profile management endpoints.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.auth import get_current_client
from app.services import customer_service, whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/customers", tags=["customers"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class CustomerOut(BaseModel):
    """Customer record returned to the dashboard."""

    id: int
    client_id: int
    phone: str
    name: Optional[str]
    email: Optional[str]
    address: Optional[str]
    total_orders: int
    total_spent: float
    last_order_at: Optional[str]
    first_message_at: Optional[str]
    last_message_at: Optional[str]
    preferred_language: str
    preferred_payment: Optional[str]
    is_vip: bool
    is_blocked: bool
    tags: Optional[str]
    notes: Optional[str]
    created_at: Optional[str]

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(cls, c) -> "CustomerOut":
        """Convert Customer ORM to schema, stringifying datetimes."""
        def _fmt(dt):
            return dt.isoformat() if dt else None

        return cls(
            id=c.id,
            client_id=c.client_id,
            phone=c.phone,
            name=c.name,
            email=c.email,
            address=c.address,
            total_orders=c.total_orders,
            total_spent=c.total_spent,
            last_order_at=_fmt(c.last_order_at),
            first_message_at=_fmt(c.first_message_at),
            last_message_at=_fmt(c.last_message_at),
            preferred_language=c.preferred_language,
            preferred_payment=c.preferred_payment,
            is_vip=c.is_vip,
            is_blocked=c.is_blocked,
            tags=c.tags,
            notes=c.notes,
            created_at=_fmt(c.created_at),
        )


class CustomerPatch(BaseModel):
    """Fields the dashboard may update on a customer."""

    name: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    preferred_language: Optional[str] = None
    preferred_payment: Optional[str] = None


class SendMessagePayload(BaseModel):
    """Payload for the send-message endpoint."""

    message: str


class CustomerStatsOut(BaseModel):
    """Aggregate stats returned to the dashboard header row."""

    total_customers: int
    active_this_month: int
    vip_customers: int
    avg_order_value: float


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CustomerOut])
async def list_customers(
    search: Optional[str] = Query(None),
    filter: Optional[str] = Query(None, description="all | vip | new | inactive"),
    sort: Optional[str] = Query("latest", description="latest | most_orders | highest_spent"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return a filtered, sorted, paginated customer list."""
    customers = await customer_service.list_customers(
        db,
        client_id=client.id,
        search=search,
        filter_vip=(filter == "vip"),
        filter_new=(filter == "new"),
        filter_inactive=(filter == "inactive"),
        sort_by=sort or "latest",
        skip=skip,
        limit=limit,
    )
    return [CustomerOut.from_orm_safe(c) for c in customers]


@router.get("/stats", response_model=CustomerStatsOut)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return aggregate customer stats for the dashboard header row."""
    return await customer_service.get_customer_stats(db, client.id)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return a single customer by ID."""
    from app.models.customer import Customer
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    return CustomerOut.from_orm_safe(c)


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: int,
    payload: CustomerPatch,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Update editable fields on a customer profile."""
    try:
        c = await customer_service.update_customer(
            db,
            customer_id=customer_id,
            client_id=client.id,
            **payload.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CustomerOut.from_orm_safe(c)


@router.post("/{customer_id}/vip", response_model=CustomerOut)
async def toggle_vip(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Toggle VIP status for a customer."""
    from app.models.customer import Customer
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    try:
        updated = await customer_service.set_vip(db, customer_id, client.id, not c.is_vip)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CustomerOut.from_orm_safe(updated)


@router.post("/{customer_id}/block", response_model=CustomerOut)
async def toggle_block(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Toggle blocked status for a customer."""
    from app.models.customer import Customer
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    try:
        updated = await customer_service.set_blocked(db, customer_id, client.id, not c.is_blocked)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CustomerOut.from_orm_safe(updated)


@router.post("/{customer_id}/message", response_model=dict)
async def send_message(
    customer_id: int,
    payload: SendMessagePayload,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Send a custom WhatsApp message to a customer."""
    from app.models.customer import Customer
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        await whatsapp_service.send_text_message(c.phone, payload.message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {exc}")

    return {"status": "sent", "phone": c.phone}


@router.get("/{customer_id}/orders", response_model=list[dict])
async def get_customer_orders(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return all orders for a specific customer."""
    from app.models.customer import Customer
    from app.models.order import Order
    from sqlalchemy import select

    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    orders_result = await db.execute(
        select(Order).where(
            Order.client_id == client.id,
            Order.customer_phone == c.phone,
        ).order_by(Order.created_at.desc())
    )
    orders = list(orders_result.scalars().all())

    def _fmt(dt):
        return dt.isoformat() if dt else None

    return [
        {
            "id": o.id,
            "order_number": o.order_number,
            "product_name": o.product_name,
            "quantity": o.quantity,
            "total_amount": o.total_amount,
            "payment_method": o.payment_method,
            "status": o.status,
            "created_at": _fmt(o.created_at),
        }
        for o in orders
    ]


@router.get("/{customer_id}/conversations", response_model=list[dict])
async def get_customer_conversations(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return all conversations for a specific customer."""
    from app.models.customer import Customer
    from app.models.conversation import Conversation

    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.client_id == client.id,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.phone_number == c.phone,
        ).order_by(Conversation.created_at.desc())
    )
    convs = list(conv_result.scalars().all())

    def _fmt(dt):
        return dt.isoformat() if dt else None

    return [
        {
            "id": cv.id,
            "channel": cv.channel,
            "current_stage": cv.current_stage,
            "created_at": _fmt(cv.created_at),
            "updated_at": _fmt(cv.updated_at),
        }
        for cv in convs
    ]
