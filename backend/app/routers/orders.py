"""
Orders API router (protected — requires JWT).

Provides the React dashboard with order management endpoints.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.auth import get_current_client
from app.services import order_service, whatsapp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class OrderOut(BaseModel):
    """Order record returned to the dashboard."""

    id: int
    order_number: str
    client_id: int
    conversation_id: Optional[int]
    customer_name: str
    customer_phone: str
    delivery_address: str
    product_id: Optional[int]
    product_name: str
    product_sku: Optional[str]
    variant_color: Optional[str]
    variant_size: Optional[str]
    quantity: int
    unit_price: float
    total_amount: float
    payment_method: str
    payment_status: str
    razorpay_payment_id: Optional[str]
    status: str
    tracking_number: Optional[str]
    courier_name: Optional[str]
    created_at: Optional[str]
    confirmed_at: Optional[str]
    paid_at: Optional[str]
    dispatched_at: Optional[str]
    delivered_at: Optional[str]
    notes: Optional[str]
    invoice_url: Optional[str]
    invoice_number: Optional[str]

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(cls, order) -> "OrderOut":
        """Convert Order ORM to schema, stringifying datetimes."""
        def _fmt(dt):
            return dt.isoformat() if dt else None

        return cls(
            id=order.id,
            order_number=order.order_number,
            client_id=order.client_id,
            conversation_id=order.conversation_id,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            delivery_address=order.delivery_address,
            product_id=order.product_id,
            product_name=order.product_name,
            product_sku=order.product_sku,
            variant_color=order.variant_color,
            variant_size=order.variant_size,
            quantity=order.quantity,
            unit_price=order.unit_price,
            total_amount=order.total_amount,
            payment_method=order.payment_method,
            payment_status=order.payment_status,
            razorpay_payment_id=order.razorpay_payment_id,
            status=order.status,
            tracking_number=order.tracking_number,
            courier_name=order.courier_name,
            created_at=_fmt(order.created_at),
            confirmed_at=_fmt(order.confirmed_at),
            paid_at=_fmt(order.paid_at),
            dispatched_at=_fmt(order.dispatched_at),
            delivered_at=_fmt(order.delivered_at),
            notes=order.notes,
            invoice_url=order.invoice_url,
            invoice_number=order.invoice_number,
        )


class OrderStatsOut(BaseModel):
    """Aggregate order stats for the dashboard."""

    today_orders: int
    today_revenue: float
    pending_dispatch: int
    cod_pending: int
    total_orders: int
    total_revenue: float


class UpdateStatusRequest(BaseModel):
    """Body for PATCH /orders/{id}/status."""

    status: str
    tracking_number: Optional[str] = None
    courier_name: Optional[str] = None
    notes: Optional[str] = None


class NotifyCustomerRequest(BaseModel):
    """Body for POST /orders/{id}/notify-customer."""

    message: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=OrderStatsOut)
async def get_stats(
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> OrderStatsOut:
    """
    Return aggregate order stats for today and all-time.

    Returns:
        OrderStatsOut with today_orders, today_revenue, pending_dispatch,
        cod_pending, total_orders, total_revenue.
    """
    stats = await order_service.get_orders_stats(client.id, db)
    return OrderStatsOut(**stats)


@router.get("/export/csv")
async def export_csv(
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Download all orders for the authenticated client as a CSV file.

    Returns:
        Streaming CSV response with Content-Disposition: attachment.
    """
    orders = await order_service.get_orders(db, client.id, limit=10000)
    csv_content = order_service.orders_to_csv(orders)

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.get("", response_model=list[OrderOut])
async def list_orders(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> list[OrderOut]:
    """
    Return paginated orders for the authenticated client.

    Query params:
        status: Filter by order status (new/confirmed/paid/processing/dispatched/delivered/cancelled).
        search: Search by order number, customer name, or phone.
        date_from / date_to: Date range filter (YYYY-MM-DD).
        skip / limit: Pagination.

    Returns:
        List of OrderOut, newest first.
    """
    orders = await order_service.get_orders(
        db, client.id,
        status=status,
        search=search,
        date_from=date_from,
        date_to=date_to,
        skip=skip,
        limit=limit,
    )
    return [OrderOut.from_orm_safe(o) for o in orders]


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: int,
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    """
    Return a single order's full details.

    Raises:
        HTTPException 404: If the order does not exist for this client.
    """
    try:
        order = await order_service.get_order(order_id, client.id, db)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return OrderOut.from_orm_safe(order)


@router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(
    order_id: int,
    body: UpdateStatusRequest,
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    """
    Update an order's status.

    When status is 'dispatched', tracking_number and courier_name are persisted
    and the customer receives a WhatsApp notification.

    Raises:
        HTTPException 404: If the order does not exist for this client.
    """
    valid_statuses = {"new", "confirmed", "paid", "processing", "dispatched", "delivered", "cancelled"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    try:
        order = await order_service.update_order_status(
            db=db,
            order_id=order_id,
            client_id=client.id,
            new_status=body.status,
            tracking_number=body.tracking_number,
            courier_name=body.courier_name,
            notes=body.notes,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return OrderOut.from_orm_safe(order)


@router.post("/{order_id}/notify-customer")
async def notify_customer(
    order_id: int,
    body: NotifyCustomerRequest,
    client=Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Send a custom WhatsApp message to the customer for a given order.

    Raises:
        HTTPException 404: If the order does not exist for this client.
    """
    try:
        order = await order_service.get_order(order_id, client.id, db)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    try:
        await whatsapp_service.send_text_message(order.customer_phone, body.message)
        return {"status": "sent"}
    except Exception as exc:
        logger.error("Failed to notify customer for order %s: %s", order.order_number, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="WhatsApp send failed.",
        )
