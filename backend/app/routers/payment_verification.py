"""
Manual UPI payment verification API (seller-side).

Endpoints:
- POST /orders/{id}/payment/approve   payment_submitted → paid
- POST /orders/{id}/payment/reject    payment_submitted → pending_payment  {reason?}
- POST /orders/{id}/payment/cancel    pending_payment|payment_submitted → cancelled  {reason?}
- GET  /orders/{id}/audit             who approved/rejected/cancelled, and when
- GET  /payments/pending              orders with proofs (status=pending|approved|rejected)
- GET  /payments/pending/count        badge count of orders awaiting verification

All endpoints require the `payment_verify` permission (Owner always has it)
and are scoped to the caller's own business.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.order import Order
from app.models.order_audit_log import OrderAuditLog
from app.models.user import User
from app.routers.auth import require_permission
from app.services import payment_verification_service as pvs
from app.services.order_state_machine import InvalidOrderTransition

router = APIRouter(tags=["payment-verification"])

_perm = require_permission("payment_verify")


class ReasonBody(BaseModel):
    """Optional free-text reason for a reject/cancel."""

    reason: Optional[str] = Field(default=None, max_length=300)


class DecisionOut(BaseModel):
    """Result of approve/reject/cancel."""

    order_id: int
    order_number: str
    status: str
    changed: bool = Field(description="False when this was an idempotent replay (nothing written or sent).")
    customer_notified: Optional[bool] = Field(
        default=None, description="Whether the customer template was delivered; null when none was attempted.")
    notify_failure: Optional[str] = Field(
        default=None, description="Why the template wasn't delivered (WINDOW_CLOSED, OPTED_OUT, …).")


class ProofOut(BaseModel):
    """One payment screenshot."""

    id: int
    message_id: Optional[int]
    media_url: str
    status: str
    created_at: Optional[str]
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    reject_reason: Optional[str]


class ItemOut(BaseModel):
    """One order line."""

    name: str
    variant: Optional[str]
    quantity: int
    subtotal: float


class PaymentRowOut(BaseModel):
    """An order awaiting (or past) verification, with its proofs."""

    order_id: int
    order_number: str
    order_status: str
    conversation_id: Optional[int]
    channel: Optional[str]
    customer_name: str
    customer_phone: str
    amount_expected: float
    currency: str = "INR"
    items: list[ItemOut]
    proofs: list[ProofOut]
    waiting_since: Optional[str]
    waiting_seconds: Optional[int]
    overdue: bool
    seller_upi_id: Optional[str]
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    reject_reason: Optional[str]


class AuditOut(BaseModel):
    """One audit-log entry."""

    id: int
    action: str
    from_status: Optional[str]
    to_status: Optional[str]
    actor: Optional[str]
    detail: Optional[dict]
    created_at: Optional[str]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 string or None."""
    return dt.isoformat() if dt else None


def _decision_out(result: pvs.DecisionResult) -> DecisionOut:
    """Map a service DecisionResult to the API model."""
    return DecisionOut(
        order_id=result.order.id, order_number=result.order.order_number,
        status=result.order.status, changed=result.changed,
        customer_notified=result.customer_notified, notify_failure=result.notify_failure,
    )


def _http_error(exc: Exception) -> HTTPException:
    """Translate service errors into the API error contract ({code, message})."""
    if isinstance(exc, pvs.OrderNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ORDER_NOT_FOUND", "message": "Order not found."})
    if isinstance(exc, InvalidOrderTransition):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "INVALID_STATE", "message": str(exc), "current": exc.current, "target": exc.target},
        )
    raise exc


@router.post("/orders/{order_id}/payment/approve", response_model=DecisionOut)
async def approve_payment(
    order_id: int, user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """Approve the customer's payment: order → paid, stock deducted once, customer told."""
    try:
        return _decision_out(await pvs.approve(db, user.client, order_id, user))
    except (pvs.OrderNotFound, InvalidOrderTransition) as exc:
        await db.rollback()
        raise _http_error(exc)


@router.post("/orders/{order_id}/payment/reject", response_model=DecisionOut)
async def reject_payment(
    order_id: int, body: ReasonBody | None = None,
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """Reject the screenshot: order → pending_payment (stock stays reserved), customer asked to resend."""
    try:
        return _decision_out(await pvs.reject(db, user.client, order_id, user, body.reason if body else None))
    except (pvs.OrderNotFound, InvalidOrderTransition) as exc:
        await db.rollback()
        raise _http_error(exc)


@router.post("/orders/{order_id}/payment/cancel", response_model=DecisionOut)
async def cancel_unpaid_order(
    order_id: int, body: ReasonBody | None = None,
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """Cancel an unpaid order: stock reservation released, customer told."""
    try:
        return _decision_out(await pvs.cancel(db, user.client, order_id, user, body.reason if body else None))
    except (pvs.OrderNotFound, InvalidOrderTransition) as exc:
        await db.rollback()
        raise _http_error(exc)


@router.get("/orders/{order_id}/audit", response_model=list[AuditOut])
async def order_audit(
    order_id: int, user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> list[AuditOut]:
    """Return the order's state-change audit trail, oldest first."""
    owned = (await db.execute(
        select(Order.id).where(Order.id == order_id, Order.client_id == user.client_id)
    )).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ORDER_NOT_FOUND", "message": "Order not found."})
    rows = (await db.execute(
        select(OrderAuditLog).where(OrderAuditLog.order_id == order_id).order_by(OrderAuditLog.id)
    )).scalars().all()
    return [
        AuditOut(id=r.id, action=r.action, from_status=r.from_status, to_status=r.to_status,
                 actor=r.actor_name, detail=r.detail, created_at=_iso(r.created_at))
        for r in rows
    ]


@router.get("/payments/pending/count")
async def pending_count(user: User = Depends(_perm), db: AsyncSession = Depends(get_db)) -> dict:
    """Number of orders awaiting verification (sidebar badge / dashboard widget)."""
    return {"count": await pvs.count_pending(db, user.client_id)}


@router.get("/payments/pending", response_model=list[PaymentRowOut])
async def list_pending_payments(
    status_filter: Literal["pending", "approved", "rejected"] = Query("pending", alias="status"),
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(_perm),
    db: AsyncSession = Depends(get_db),
) -> list[PaymentRowOut]:
    """
    List orders with payment proofs.

    status=pending (default): orders currently payment_submitted, oldest-waiting first.
    status=approved|rejected: orders with a proof carrying that decision, newest first.
    """
    rows = await pvs.list_payments(
        db, user.client_id, status=status_filter, date_from=date_from, date_to=date_to,
        search=search, limit=limit, offset=offset,
    )
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        o = r.order
        since = r.waiting_since
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        waiting = int((now - since).total_seconds()) if since and status_filter == "pending" else None
        reviewed = next((p for p in reversed(r.proofs) if p.reviewed_at), None)
        items = o.line_items or []
        out.append(PaymentRowOut(
            order_id=o.id, order_number=o.order_number, order_status=o.status,
            conversation_id=o.conversation_id, channel=r.channel,
            customer_name=o.customer_name, customer_phone=o.customer_phone,
            amount_expected=o.total_amount,
            items=[
                ItemOut(
                    name=li.product_name,
                    variant="/".join(p for p in (li.variant_color, li.variant_size, li.variant_material) if p) or None,
                    quantity=li.quantity, subtotal=li.subtotal,
                ) for li in items
            ] or [ItemOut(name=o.product_name, variant=None, quantity=o.quantity, subtotal=o.total_amount)],
            proofs=[
                ProofOut(
                    id=p.id, message_id=p.message_id, media_url=p.media_url, status=p.status,
                    created_at=_iso(p.created_at), reviewed_by=p.reviewed_by_name,
                    reviewed_at=_iso(p.reviewed_at), reject_reason=p.reject_reason,
                ) for p in r.proofs
            ],
            waiting_since=_iso(since), waiting_seconds=waiting,
            overdue=bool(waiting is not None and waiting > pvs.OVERDUE_AFTER.total_seconds()),
            seller_upi_id=user.client.upi_id,
            reviewed_by=reviewed.reviewed_by_name if reviewed else None,
            reviewed_at=_iso(reviewed.reviewed_at) if reviewed else None,
            reject_reason=reviewed.reject_reason if reviewed else None,
        ))
    return out
