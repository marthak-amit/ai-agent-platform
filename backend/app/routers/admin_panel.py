"""
Admin panel data endpoints (all under /admin/panel; every route declares the permission it needs).

  GET   /overview                                  landing-page numbers                      overview.read
  GET   /clients                                   searchable, paginated client directory    clients.read
  GET   /clients/{id}                              full support view of one tenant           clients.read
  PATCH /clients/{id}/flags                        router_v2 / limits / grandfathering       clients.write
  GET   /clients/{id}/conversations                the tenant's inbox (admin view)           conversations.read
  GET   /clients/{id}/conversations/{cid}/messages one transcript (audited)                  conversations.read
  GET   /clients/{id}/orders                       the tenant's orders                       clients.read
  POST  /clients/{id}/impersonate                  30-minute tenant token (reason, audited)  clients.impersonate
  GET   /billing-plans, PUT /billing-plans/{id}    sellable plans                            billing.read / plans.write
  GET   /system                                    live platform health                      system.read

Suspend/activate, plan (legacy `plans`) edits, LLM usage and the billing operations (grants, extensions,
payment events…) keep their existing routes under /admin and /admin/billing.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.admin_deps import require_perm
from app.schemas.admin_panel import (
    AuditLogOut,
    BillingPlanOut,
    BillingPlanUpdate,
    ClientDetailOut,
    ClientFlagsUpdate,
    ClientSearchOut,
    ConversationPage,
    FlagsChangedOut,
    ImpersonateOut,
    ImpersonateRequest,
    MessageRowOut,
    OrderPage,
    OverviewOut,
    SystemHealthOut,
)
from app.services import admin_audit, admin_panel_service
from app.services.admin_audit import AdminPrincipal
from app.models.client import Client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/panel", tags=["admin-panel"])


def _not_found(what: str) -> HTTPException:
    """404 with the panel's {code, message} body."""
    return HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": f"{what} not found."})


async def _client_or_404(db: AsyncSession, client_id: int) -> Client:
    """Load a client or raise 404."""
    client = await admin_panel_service.get_client(db, client_id)
    if client is None:
        raise _not_found("Client")
    return client


@router.get("/overview", response_model=OverviewOut, dependencies=[Depends(require_perm("overview.read"))])
async def overview(db: AsyncSession = Depends(get_db)) -> dict:
    """Tenants, revenue, traffic, LLM spend (30 d) and the counters that need a human."""
    return await admin_panel_service.get_overview(db)


@router.get("/clients", response_model=ClientSearchOut, dependencies=[Depends(require_perm("clients.read"))])
async def client_directory(
    q: str = Query("", max_length=100, description="id, email, business name or phone"),
    status_filter: str = Query("", alias="status", pattern="^(active|suspended)?$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search and page through every client with its current subscription."""
    return await admin_panel_service.search_clients(db, q=q, status_filter=status_filter, page=page, page_size=page_size)


@router.get("/clients/{client_id}", response_model=ClientDetailOut, dependencies=[Depends(require_perm("clients.read"))])
async def client_detail(client_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """One tenant: profile, flags, channel status (no secrets), billing, team, counts, usage and recent admin actions."""
    client = await _client_or_404(db, client_id)
    detail = await admin_panel_service.get_client_detail(db, client)
    audit = await admin_panel_service.recent_client_audit(db, client_id)
    detail["recent_audit"] = [AuditLogOut.model_validate(a) for a in audit]
    return detail


@router.patch("/clients/{client_id}/flags", response_model=FlagsChangedOut)
async def update_flags(
    client_id: int,
    body: ClientFlagsUpdate,
    principal: Annotated[AdminPrincipal, Depends(require_perm("clients.write"))],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change operational flags. Only fields present in the request body change (null is meaningful for router_v2_enabled)."""
    client = await _client_or_404(db, client_id)
    sent = body.model_dump(exclude_unset=True)
    reason = sent.pop("reason", "") or ""
    return await admin_panel_service.update_client_flags(db, client, sent, principal, reason)


@router.get(
    "/clients/{client_id}/conversations", response_model=ConversationPage,
    dependencies=[Depends(require_perm("conversations.read"))],
)
async def client_conversations(
    client_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The tenant's real conversations (sandbox excluded), most recently active first."""
    await _client_or_404(db, client_id)
    return await admin_panel_service.list_client_conversations(db, client_id, page=page, page_size=page_size)


@router.get("/clients/{client_id}/conversations/{conversation_id}/messages", response_model=list[MessageRowOut])
async def conversation_messages(
    client_id: int,
    conversation_id: int,
    principal: Annotated[AdminPrincipal, Depends(require_perm("conversations.read"))],
    db: AsyncSession = Depends(get_db),
) -> list:
    """A transcript (newest 200 messages). Reading customer chats is itself audited."""
    messages = await admin_panel_service.get_conversation_messages(db, client_id, conversation_id)
    if messages is None:
        raise _not_found("Conversation")
    admin_audit.record(
        db, principal, "conversation.view", target_type="conversation", target_id=conversation_id, client_id=client_id
    )
    await db.commit()
    return messages


@router.get(
    "/clients/{client_id}/orders", response_model=OrderPage, dependencies=[Depends(require_perm("clients.read"))]
)
async def client_orders(
    client_id: int,
    status_filter: str = Query("", alias="status", max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The tenant's orders, newest first."""
    await _client_or_404(db, client_id)
    return await admin_panel_service.list_client_orders(db, client_id, status_filter=status_filter, page=page, page_size=page_size)


@router.post("/clients/{client_id}/impersonate", response_model=ImpersonateOut)
async def impersonate(
    client_id: int,
    body: ImpersonateRequest,
    principal: Annotated[AdminPrincipal, Depends(require_perm("clients.impersonate"))],
    db: AsyncSession = Depends(get_db),
) -> ImpersonateOut:
    """Mint a 30-minute token that signs in as the client's owner. A reason is mandatory and recorded."""
    client = await _client_or_404(db, client_id)
    try:
        token, email = await admin_panel_service.issue_impersonation_token(db, client, principal, body.reason)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "cannot_impersonate", "message": str(exc)}) from exc
    return ImpersonateOut(
        access_token=token, expires_in=admin_panel_service.IMPERSONATION_MINUTES * 60, client_id=client_id, email=email
    )


@router.get("/billing-plans", response_model=list[BillingPlanOut], dependencies=[Depends(require_perm("billing.read"))])
async def billing_plans(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Every sellable plan (including inactive) with its active-subscriber count."""
    return await admin_panel_service.list_billing_plans(db)


@router.put("/billing-plans/{plan_id}", response_model=BillingPlanOut)
async def update_billing_plan(
    plan_id: int,
    body: BillingPlanUpdate,
    principal: Annotated[AdminPrincipal, Depends(require_perm("plans.write"))],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit a sellable plan. Paid periods keep their snapshotted limits; changes apply to future purchases."""
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    reason = updates.pop("reason")
    if not updates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "no_changes", "message": "Nothing to update."})
    result = await admin_panel_service.update_billing_plan(db, plan_id, updates, principal, reason)
    if result is None:
        raise _not_found("Plan")
    return result


@router.get("/system", response_model=SystemHealthOut, dependencies=[Depends(require_perm("system.read"))])
async def system_health(db: AsyncSession = Depends(get_db)) -> dict:
    """Database, LLM breaker, Instagram verdicts, scheduler jobs, non-secret config and billing pipeline health."""
    return await admin_panel_service.get_system_health(db)
