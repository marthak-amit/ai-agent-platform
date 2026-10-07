"""
Internal billing admin endpoints (X-Admin-Key, same auth as /admin/*).

  GET  /admin/billing/subscriptions               list periods (filter client_id / status), paginated
  POST /admin/billing/subscriptions/grant         record an offline payment as a plan period (client in the body)
  POST /admin/billing/clients/{id}/grant          same, client from the path: {plan_code, days?, reason, amount_paise?}
                                                  — a paid ₹0 order (+ tax invoice only when amount_paise is given)
  POST /admin/billing/subscriptions/{id}/extend   extend an active/pending period by N days: {days, reason}
  POST /admin/billing/subscriptions/{id}/revoke   cut an active/pending period short now: {reason}
  PUT  /admin/billing/clients/{id}/billing-exempt set / clear the billing-exempt flag
  GET  /admin/billing/payment-events              stored Razorpay webhook events (filter has_error / processed / type)
  GET  /admin/billing/payment-events/{id}         one event incl. the raw payload
  POST /admin/billing/payment-events/{id}/reprocess  re-run a failed / unfinished event
  POST /admin/billing/invoices/backfill           invoices for paid orders that have none (pre-invoicing payments)

Every mutating call needs a `reason` (the old field name `note` is still accepted) and is recorded in
billing_admin_log with that reason and the actor from the optional X-Admin-User header (default "admin-key").
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.models.client import Client
from app.models.sellertalk24_billing import BillingPlan, ClientSubscription, PaymentEvent
from app.routers.admin import require_admin
from app.routers import billing as billing_router
from app.routers.billing import get_gateway
from app.schemas.sellertalk24_billing import (
    AdminBackfillOut,
    AdminClientGrantRequest,
    AdminExemptRequest,
    AdminExtendRequest,
    AdminGrantRequest,
    AdminPaymentEventListOut,
    AdminPaymentEventOut,
    AdminReprocessOut,
    AdminRevokeRequest,
    AdminSubscriptionListOut,
    AdminSubscriptionOut,
)
from app.services.billing import admin_ops, invoices
from app.services.billing.razorpay_client import BillingGateway
from app.services.billing.webhook import EventNotFound, EventNotReprocessable, reprocess_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/billing", tags=["admin-billing"], dependencies=[Depends(require_admin)])


async def admin_actor(x_admin_user: Annotated[Optional[str], Header()] = None) -> str:
    """
    Who is calling. Admin auth is a shared X-Admin-Key, so the person identifies themselves with an optional
    X-Admin-User header (recorded in the audit log, capped at 100 chars); without it the actor is "admin-key".
    """
    cleaned = (x_admin_user or "").strip()[:100]
    return cleaned or "admin-key"


def _op_error(exc: admin_ops.AdminOpError) -> HTTPException:
    """Map an AdminOpError to a {code, message} HTTPException."""
    return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})


async def _subscription_out(db: AsyncSession, sub_id: int) -> AdminSubscriptionOut:
    """Load one subscription joined with its client and plan."""
    page = await _list_subscriptions(db, sub_id=sub_id, client_id=None, sub_status=None, page=1, page_size=1)
    return page.items[0]


async def _list_subscriptions(
    db: AsyncSession, *, sub_id: int | None, client_id: int | None, sub_status: str | None, page: int, page_size: int
) -> AdminSubscriptionListOut:
    """Subscriptions joined with client + plan, newest period first."""
    filters = []
    if sub_id is not None:
        filters.append(ClientSubscription.id == sub_id)
    if client_id is not None:
        filters.append(ClientSubscription.client_id == client_id)
    if sub_status:
        filters.append(ClientSubscription.status == sub_status)
    total = await db.scalar(select(func.count()).select_from(ClientSubscription).where(*filters))
    rows = (
        await db.execute(
            select(ClientSubscription, Client.email, Client.business_name, Client.billing_exempt, BillingPlan.code, BillingPlan.name)
            .join(Client, Client.id == ClientSubscription.client_id)
            .join(BillingPlan, BillingPlan.id == ClientSubscription.plan_id)
            .where(*filters)
            .order_by(ClientSubscription.current_period_start.desc(), ClientSubscription.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    items = [
        AdminSubscriptionOut(
            id=s.id, client_id=s.client_id, client_email=email, business_name=business or "",
            billing_exempt=bool(exempt), plan_code=code, plan_name=name, status=s.status,
            current_period_start=s.current_period_start, current_period_end=s.current_period_end,
            conversations_used=s.conversations_used, conversation_limit=s.conversation_limit,
            over_limit=bool(s.over_limit), source_payment_id=s.source_payment_id, created_at=s.created_at,
        )
        for s, email, business, exempt, code, name in rows
    ]
    return AdminSubscriptionListOut(items=items, total=int(total or 0), page=page, page_size=page_size)


@router.get("/subscriptions", response_model=AdminSubscriptionListOut)
async def list_subscriptions(
    client_id: Optional[int] = Query(None),
    sub_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> AdminSubscriptionListOut:
    """All subscription periods across tenants (optionally one client / one status), newest first."""
    return await _list_subscriptions(
        db, sub_id=None, client_id=client_id, sub_status=sub_status, page=page, page_size=page_size
    )


async def _grant(
    db: AsyncSession, gateway: BillingGateway, settings: Settings, actor: str, *, client_id: int, plan_code: str,
    days: Optional[int], amount_paise: Optional[int], reason: str,
) -> AdminSubscriptionOut:
    """Shared by both grant routes."""
    try:
        sub = await admin_ops.grant_subscription(
            db, client_id=client_id, plan_code=plan_code, days=days, reason=reason, actor=actor,
            mode=gateway.mode, amount_paise=amount_paise, settings=settings,
        )
    except admin_ops.AdminOpError as exc:
        raise _op_error(exc) from exc
    return await _subscription_out(db, sub.id)


@router.post("/subscriptions/grant", response_model=AdminSubscriptionOut, status_code=status.HTTP_201_CREATED)
async def grant_subscription(
    body: AdminGrantRequest, gateway: Annotated[BillingGateway, Depends(get_gateway)],
    settings: Annotated[Settings, Depends(billing_router.get_settings)], actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> AdminSubscriptionOut:
    """Record an offline payment (client in the body): stack a period of the plan after the client's current ones."""
    return await _grant(
        db, gateway, settings, actor, client_id=body.client_id, plan_code=body.plan_code, days=body.days,
        amount_paise=body.amount_paise, reason=body.reason,
    )


@router.post("/clients/{client_id}/grant", response_model=AdminSubscriptionOut, status_code=status.HTTP_201_CREATED)
async def grant_client_subscription(
    client_id: int, body: AdminClientGrantRequest, gateway: Annotated[BillingGateway, Depends(get_gateway)],
    settings: Annotated[Settings, Depends(billing_router.get_settings)], actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> AdminSubscriptionOut:
    """
    Grant a plan to one client for an offline payment. Creates a paid ₹0 payment order (or one of `amount_paise`
    with a tax invoice when given) and a period stacked after whatever the client already has.
    """
    return await _grant(
        db, gateway, settings, actor, client_id=client_id, plan_code=body.plan_code, days=body.days,
        amount_paise=body.amount_paise, reason=body.reason,
    )


@router.post("/subscriptions/{subscription_id}/extend", response_model=AdminSubscriptionOut)
async def extend_subscription(
    subscription_id: int, body: AdminExtendRequest, actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> AdminSubscriptionOut:
    """Push an active/pending period's end out by *days*; queued periods after it shift too."""
    try:
        sub = await admin_ops.extend_subscription(
            db, subscription_id=subscription_id, days=body.days, reason=body.reason, actor=actor
        )
    except admin_ops.AdminOpError as exc:
        raise _op_error(exc) from exc
    return await _subscription_out(db, sub.id)


@router.post("/subscriptions/{subscription_id}/revoke", response_model=AdminSubscriptionOut)
async def revoke_subscription(
    subscription_id: int, body: AdminRevokeRequest, actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> AdminSubscriptionOut:
    """Cut an active/pending period short now; the tenant enters the normal grace window."""
    try:
        sub = await admin_ops.revoke_subscription(db, subscription_id=subscription_id, reason=body.reason, actor=actor)
    except admin_ops.AdminOpError as exc:
        raise _op_error(exc) from exc
    return await _subscription_out(db, sub.id)


@router.put("/clients/{client_id}/billing-exempt")
async def set_billing_exempt(
    client_id: int, body: AdminExemptRequest, actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Exempt (or un-exempt) a client from billing restrictions."""
    try:
        client = await admin_ops.set_billing_exempt(
            db, client_id=client_id, exempt=body.exempt, reason=body.reason, actor=actor
        )
    except admin_ops.AdminOpError as exc:
        raise _op_error(exc) from exc
    return {"client_id": client.id, "billing_exempt": bool(client.billing_exempt)}


def _event_out(e: PaymentEvent, *, with_payload: bool) -> AdminPaymentEventOut:
    """ORM row -> schema; the raw payload only when asked for."""
    return AdminPaymentEventOut(
        id=e.id, razorpay_event_id=e.razorpay_event_id, event_type=e.event_type, processed=e.processed,
        error=e.error, created_at=e.created_at, processed_at=e.processed_at,
        payload=e.payload if with_payload else None,
    )


@router.get("/payment-events", response_model=AdminPaymentEventListOut)
async def list_payment_events(
    has_error: Optional[bool] = Query(None, description="true = only events with an error recorded"),
    processed: Optional[bool] = Query(None),
    event_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> AdminPaymentEventListOut:
    """Stored webhook events, newest first (no payloads — fetch one event for those)."""
    filters = []
    if has_error is True:
        filters.append(PaymentEvent.error.is_not(None))
    elif has_error is False:
        filters.append(PaymentEvent.error.is_(None))
    if processed is not None:
        filters.append(PaymentEvent.processed.is_(processed))
    if event_type:
        filters.append(PaymentEvent.event_type == event_type)
    total = await db.scalar(select(func.count()).select_from(PaymentEvent).where(*filters))
    rows = (
        await db.execute(
            select(PaymentEvent).where(*filters)
            .order_by(PaymentEvent.created_at.desc(), PaymentEvent.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return AdminPaymentEventListOut(
        items=[_event_out(e, with_payload=False) for e in rows], total=int(total or 0), page=page, page_size=page_size
    )


@router.get("/payment-events/{event_id}", response_model=AdminPaymentEventOut)
async def get_payment_event(event_id: int, db: AsyncSession = Depends(get_db)) -> AdminPaymentEventOut:
    """One webhook event including its raw payload."""
    event = await db.get(PaymentEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "event_not_found", "message": "Unknown event."})
    return _event_out(event, with_payload=True)


@router.post("/payment-events/{event_id}/reprocess", response_model=AdminReprocessOut)
async def reprocess_payment_event(
    event_id: int,
    gateway: Annotated[BillingGateway, Depends(get_gateway)],
    actor: Annotated[str, Depends(admin_actor)],
    db: AsyncSession = Depends(get_db),
) -> AdminReprocessOut:
    """Re-run a failed or unfinished webhook event through the normal handlers (they are idempotent)."""
    try:
        outcome, error, processed = await reprocess_event(db, gateway, event_id)
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "event_not_found", "message": "Unknown event."}) from exc
    except EventNotReprocessable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "already_processed", "message": "This event was processed without error; nothing to redo."},
        ) from exc
    admin_ops.log_admin_action(db, "reprocess_payment_event", actor=actor, payment_event_id=event_id, outcome=outcome, error=error)
    await db.commit()
    return AdminReprocessOut(outcome=outcome, processed=processed, error=error)


@router.post("/invoices/backfill", response_model=AdminBackfillOut)
async def backfill_invoices(actor: Annotated[str, Depends(admin_actor)], db: AsyncSession = Depends(get_db)) -> AdminBackfillOut:
    """Issue invoices for paid orders that predate invoicing (oldest first, so numbers follow payment order)."""
    created = await invoices.backfill_missing_invoices(db)
    if created:
        admin_ops.log_admin_action(db, "backfill_invoices", actor=actor, count=len(created))
        await db.commit()
    return AdminBackfillOut(created=len(created), invoice_numbers=[i.invoice_number for i in created])
