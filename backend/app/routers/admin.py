"""
Admin control panel router.

All endpoints require the X-Admin-Key header to match ADMIN_SECRET_KEY from
environment. This is intentionally separate from the client JWT system so
that a leaked client token cannot grant admin access.

Endpoints:
- GET  /admin/clients              — all clients with usage and plan info
- GET  /admin/stats                — platform-wide aggregate statistics
- PUT  /admin/clients/{id}/suspend — suspend a client account
- PUT  /admin/clients/{id}/activate— activate a suspended client account
- GET  /admin/revenue              — monthly revenue breakdown by plan
"""

import logging
import secrets
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.services import admin_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Auth dependency ────────────────────────────────────────────────────────────

async def require_admin(x_admin_key: Optional[str] = Header(default=None)) -> None:
    """
    FastAPI dependency that enforces admin authentication.

    Reads ADMIN_SECRET_KEY from settings and compares it with the
    X-Admin-Key request header using a timing-safe digest comparison
    to prevent timing-attack disclosure of the key.

    Args:
        x_admin_key: Value of the X-Admin-Key header, or None if absent.

    Raises:
        HTTPException 401: If the header is missing or the key is wrong.
    """
    settings = get_settings()
    if not x_admin_key or not secrets.compare_digest(
        x_admin_key.encode("utf-8"),
        settings.admin_secret_key.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key.",
        )


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
    dependencies=[Depends(require_admin)],
)
async def list_all_clients(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Return all registered clients with their current usage and plan revenue.

    Requires X-Admin-Key header.

    Returns:
        List of ClientAdminOut objects ordered by client id.
    """
    return await admin_service.get_all_clients(db)


@router.get(
    "/stats",
    response_model=PlatformStatsOut,
    dependencies=[Depends(require_admin)],
)
async def platform_stats(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return platform-wide aggregate statistics.

    Requires X-Admin-Key header.

    Returns:
        PlatformStatsOut with active_clients, monthly_revenue_inr,
        messages_today, messages_this_month.
    """
    return await admin_service.get_platform_stats(db)


@router.put(
    "/clients/{client_id}/suspend",
    response_model=ClientStatusOut,
    dependencies=[Depends(require_admin)],
)
async def suspend_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
) -> ClientStatusOut:
    """
    Suspend a client account (sets is_active=False).

    The client's AI agent will stop responding to messages once the webhook
    can no longer find an active client. The client's data is preserved.

    Requires X-Admin-Key header.

    Args:
        client_id: Target client primary key.

    Returns:
        ClientStatusOut confirming the suspension.

    Raises:
        HTTPException 404: If client_id does not exist.
    """
    try:
        client = await admin_service.set_client_active(db, client_id, active=False)
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
    dependencies=[Depends(require_admin)],
)
async def activate_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
) -> ClientStatusOut:
    """
    Re-activate a previously suspended client account.

    Requires X-Admin-Key header.

    Args:
        client_id: Target client primary key.

    Returns:
        ClientStatusOut confirming the activation.

    Raises:
        HTTPException 404: If client_id does not exist.
    """
    try:
        client = await admin_service.set_client_active(db, client_id, active=True)
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
    dependencies=[Depends(require_admin)],
)
async def revenue_breakdown(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return this month's projected revenue broken down by plan tier.

    Revenue is subscription-based (active clients × plan price). All three
    tiers are always present in the breakdown, even when count is zero.

    Requires X-Admin-Key header.

    Returns:
        RevenueOut with month, total_revenue_inr, and per-plan breakdown.
    """
    return await admin_service.get_revenue_breakdown(db)
