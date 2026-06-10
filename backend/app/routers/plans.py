"""
Plan management router.

Endpoints:
- GET  /plans         — list all available plans
- GET  /plans/current — return the authenticated client's active plan
- POST /plans/upgrade — upgrade the client to a higher-tier plan
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import plan_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plans", tags=["plans"])


# ── schemas ───────────────────────────────────────────────────────────────────

class PlanOut(BaseModel):
    """Public representation of a plan."""

    slug: str
    name: str
    price_inr: int
    daily_msg_limit: int
    channels: list[str]
    description: str


class UpgradeRequest(BaseModel):
    """Request body for POST /plans/upgrade."""

    plan_slug: str


class UpgradeResponse(BaseModel):
    """Response for a successful plan upgrade."""

    previous_plan: str
    new_plan: PlanOut
    message: str


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[PlanOut])
async def list_plans() -> list[dict[str, Any]]:
    """
    Return all available plans in tier order (starter → growth → pro).

    This endpoint is public — no authentication required.

    Returns:
        List of PlanOut objects.
    """
    return plan_service.list_plans()


@router.get("/current", response_model=PlanOut)
async def get_current_plan(
    current_client: Annotated[Client, Depends(get_current_client)],
) -> dict[str, Any]:
    """
    Return the authenticated client's active plan details.

    Args:
        current_client: JWT-authenticated Client.

    Returns:
        PlanOut for the client's current plan_slug.
    """
    plan = plan_service.get_plan(current_client.plan_slug or "starter")
    if plan is None:
        # Fallback — should not happen in production
        plan = plan_service.get_plan("starter")
    return plan


@router.post("/upgrade", response_model=UpgradeResponse)
async def upgrade_plan(
    body: UpgradeRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> UpgradeResponse:
    """
    Upgrade the authenticated client's plan to a higher tier.

    Only upgrades are permitted through this endpoint (starter→growth,
    starter→pro, growth→pro). For downgrades, contact support.

    Args:
        body:           Target plan_slug.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        UpgradeResponse with previous_plan, new_plan details, and a message.

    Raises:
        HTTPException 400: If the target plan is unknown or not an upgrade.
    """
    previous_slug = current_client.plan_slug or "starter"
    try:
        new_plan = await plan_service.upgrade_plan(db, current_client, body.plan_slug)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    logger.info(
        "Client %d upgraded plan: %s → %s", current_client.id, previous_slug, body.plan_slug
    )
    return UpgradeResponse(
        previous_plan=previous_slug,
        new_plan=PlanOut(**new_plan),
        message=f"Successfully upgraded to {new_plan['name']} plan.",
    )
