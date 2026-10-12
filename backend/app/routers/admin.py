"""
Admin control panel router.

Authentication is the operator JWT (Authorization: Bearer, see admin_auth.py) or the
legacy X-Admin-Key header (see admin_deps.py) — both intentionally separate from the
client JWT system so that a leaked client token cannot grant admin access. Each route
declares the permission it needs (RBAC) and every mutation writes an admin_audit_log row.

Endpoints:
- GET  /admin/clients              — all clients with usage and plan info
- GET  /admin/stats                — platform-wide aggregate statistics
- PUT  /admin/clients/{id}/suspend — suspend a client account
- PUT  /admin/clients/{id}/activate— activate a suspended client account
- GET  /admin/revenue              — monthly revenue breakdown by plan
- GET  /admin/usage/llm            — LLM token/cost rollup across all clients (optional client_id)
- GET  /admin/plans                — all plans, including inactive ones
- PUT  /admin/plans/{plan_id}      — update a plan's price/limits/overage rate
"""

import logging
from datetime import date, datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.admin_deps import require_admin, require_perm  # noqa: F401  (require_admin re-exported)
from app.services.admin_audit import AdminPrincipal
from app.schemas.llm_usage import LLMUsageReport
from app.schemas.plan import PlanAdminOut, PlanUpdateRequest
from app.services import admin_service, llm_usage_service, plan_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ClientAdminOut(BaseModel):
    """Per-client row in the admin client list."""

    id: int
    email: str
    business_name: str
    plan_slug: str
    is_active: bool
    messages_today: int
    messages_this_month: int
    monthly_revenue_inr: int
    created_at: Optional[datetime]


class PlatformStatsOut(BaseModel):
    """Aggregate platform statistics."""

    active_clients: int
    monthly_revenue_inr: int
    messages_today: int
    messages_this_month: int


class ReasonBody(BaseModel):
    """Optional free-text reason recorded in the audit log."""

    reason: str = Field(default="", max_length=1000)


class ClientStatusOut(BaseModel):
    """Response after suspend or activate."""

    id: int
    email: str
    is_active: bool
    message: str


class RevenueBreakdownRow(BaseModel):
    """Revenue contribution of a single plan tier."""

    plan: str
    plan_name: str
    client_count: int
    revenue_inr: int


class RevenueOut(BaseModel):
    """Monthly revenue breakdown across all plan tiers."""

    month: str
    total_revenue_inr: int
    breakdown: list[RevenueBreakdownRow]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "/clients",
    response_model=list[ClientAdminOut],
    dependencies=[Depends(require_perm("clients.read"))],
)
async def list_all_clients(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Return all registered clients with their current usage and plan revenue.

    Requires an admin credential with the matching permission.

    Returns:
        List of ClientAdminOut objects ordered by client id.
    """
    return await admin_service.get_all_clients(db)


@router.get(
    "/stats",
    response_model=PlatformStatsOut,
    dependencies=[Depends(require_perm("overview.read"))],
)
async def platform_stats(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return platform-wide aggregate statistics.

    Requires an admin credential with the matching permission.

    Returns:
        PlatformStatsOut with active_clients, monthly_revenue_inr,
        messages_today, messages_this_month.
    """
    return await admin_service.get_platform_stats(db)


@router.put(
    "/clients/{client_id}/suspend",
    response_model=ClientStatusOut,
)
async def suspend_client(
    client_id: int,
    principal: Annotated[AdminPrincipal, Depends(require_perm("clients.write"))],
    body: Annotated[Optional[ReasonBody], Body()] = None,
    db: AsyncSession = Depends(get_db),
) -> ClientStatusOut:
    """
    Suspend a client account (sets is_active=False).

    The client's AI agent will stop responding to messages once the webhook
    can no longer find an active client. The client's data is preserved.

    Requires an admin credential with the matching permission.

    Args:
        client_id: Target client primary key.

    Returns:
        ClientStatusOut confirming the suspension.

    Raises:
        HTTPException 404: If client_id does not exist.
    """
    try:
        client = await admin_service.set_client_active(
            db, client_id, active=False, audit=(principal, body.reason if body else "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    logger.warning("Admin suspended client %d (%s).", client_id, client.email)
    return ClientStatusOut(
        id=client.id,
        email=client.email,
        is_active=client.is_active,
        message=f"Client {client.email} has been suspended.",
    )


@router.put(
    "/clients/{client_id}/activate",
    response_model=ClientStatusOut,
)
async def activate_client(
    client_id: int,
    principal: Annotated[AdminPrincipal, Depends(require_perm("clients.write"))],
    body: Annotated[Optional[ReasonBody], Body()] = None,
    db: AsyncSession = Depends(get_db),
) -> ClientStatusOut:
    """
    Re-activate a previously suspended client account.

    Requires an admin credential with the matching permission.

    Args:
        client_id: Target client primary key.

    Returns:
        ClientStatusOut confirming the activation.

    Raises:
        HTTPException 404: If client_id does not exist.
    """
    try:
        client = await admin_service.set_client_active(
            db, client_id, active=True, audit=(principal, body.reason if body else "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    logger.info("Admin activated client %d (%s).", client_id, client.email)
    return ClientStatusOut(
        id=client.id,
        email=client.email,
        is_active=client.is_active,
        message=f"Client {client.email} has been activated.",
    )


@router.get(
    "/revenue",
    response_model=RevenueOut,
    dependencies=[Depends(require_perm("billing.read"))],
)
async def revenue_breakdown(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return this month's projected revenue broken down by plan tier.

    Revenue is subscription-based (active clients × plan price). All three
    tiers are always present in the breakdown, even when count is zero.

    Requires an admin credential with the matching permission.

    Returns:
        RevenueOut with month, total_revenue_inr, and per-plan breakdown.
    """
    return await admin_service.get_revenue_breakdown(db)


@router.get(
    "/usage/llm",
    response_model=LLMUsageReport,
    dependencies=[Depends(require_perm("usage.read"))],
)
async def llm_usage_report(
    client_id: Optional[int] = Query(None, description="Restrict to one client; omit for all clients."),
    from_date: Optional[date] = Query(None, alias="from", description="Inclusive start date (default: 29 days before `to`)."),
    to_date: Optional[date] = Query(None, alias="to", description="Inclusive end date (default: today)."),
    limit: int = Query(100, ge=1, le=500, description="Max rows in per_conversation / per_order."),
    db: AsyncSession = Depends(get_db),
) -> LLMUsageReport:
    """
    LLM tokens, ₹ cost, latency and failures from the llm_usage ledger, platform-wide or per client.

    Requires an admin credential with the matching permission.
    """
    report = await llm_usage_service.usage_report(db, client_id=client_id, start=from_date, end=to_date, limit=limit)
    return LLMUsageReport(client_id=client_id, **report)


@router.get(
    "/plans",
    response_model=list[PlanAdminOut],
    dependencies=[Depends(require_perm("billing.read"))],
)
async def list_all_plans(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Return every plan, including inactive ones, in tier order.

    Requires an admin credential with the matching permission.

    Returns:
        List of PlanAdminOut objects.
    """
    return await plan_cache.get_all_plans(db)


@router.put(
    "/plans/{plan_id}",
    response_model=PlanAdminOut,
)
async def update_plan(
    plan_id: str,
    body: PlanUpdateRequest,
    principal: Annotated[AdminPrincipal, Depends(require_perm("plans.write"))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Update a plan's price, limits, channels, or overage rate.

    Takes effect immediately for new signups. Existing clients pick up the
    change on their next billing cycle rollover (or immediately if they
    upgrade) — never retroactively mid-cycle, unless separately upgraded.
    Invalidates the in-process plan cache so the change is visible without
    a redeploy.

    Requires an admin credential with the matching permission.

    Args:
        plan_id: Target plan's primary key (e.g. "starter").
        body:    Fields to update; unset fields are left unchanged.
        db:      Injected async DB session.

    Returns:
        The updated plan as a dict (validated against PlanAdminOut).

    Raises:
        HTTPException 404: If plan_id does not exist.
    """
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        plan = await admin_service.update_plan(db, plan_id, updates, audit=principal)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    logger.info("Admin updated plan '%s': %s", plan_id, updates)
    return {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "price_inr": plan.price_inr,
        "conv_limit": plan.conv_limit,
        "image_quota": plan.image_quota,
        "image_overage_price": plan.image_overage_price,
        "daily_msg_limit": plan.daily_msg_limit,
        "channels": plan.channels,
        "campaign_allowed": plan.campaign_allowed,
        "campaign_max_recipients": plan.campaign_max_recipients,
        "campaign_monthly_limit": plan.campaign_monthly_limit,
        "tier_order": plan.tier_order,
        "description": plan.description,
        "is_active": plan.is_active,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }
