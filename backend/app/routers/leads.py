"""
Leads API router (protected — requires JWT).

Provides the React dashboard with lead data filtered by status.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.lead import Lead
from app.routers.auth import get_current_client

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/leads",
    tags=["leads"],
    dependencies=[Depends(get_current_client)],
)


class LeadOut(BaseModel):
    """Lead record returned to the dashboard."""

    id: int
    phone_number: str
    status: str

    model_config = {"from_attributes": True}


class UpdateLeadRequest(BaseModel):
    """Manual lead status override from the dashboard."""

    status: str


@router.get("", response_model=list[LeadOut])
async def list_leads(
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, le=500),
) -> list[Lead]:
    """
    Return all leads, optionally filtered by status.

    Args:
        current_client: Authenticated client (JWT guard).
        db:             Injected async DB session.
        status_filter:  Filter by 'hot', 'warm', or 'cold'.
        limit:          Max rows.

    Returns:
        List of LeadOut objects.
    """
    query = select(Lead).order_by(Lead.updated_at.desc().nullslast()).limit(limit)
    if status_filter:
        query = query.where(Lead.status == status_filter)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.patch("/{lead_id}", response_model=LeadOut)
async def update_lead_status(
    lead_id: int,
    body: UpdateLeadRequest,
    db: AsyncSession = Depends(get_db),
) -> Lead:
    """
    Manually override a lead's status from the dashboard.

    Args:
        lead_id: Lead primary key.
        body:    New status value ('hot', 'warm', or 'cold').

    Returns:
        Updated LeadOut.
    """
    from fastapi import HTTPException, status as http_status

    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Lead not found.")

    if body.status not in ("hot", "warm", "cold"):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be hot, warm, or cold.",
        )

    lead.status = body.status
    await db.commit()
    await db.refresh(lead)
    return lead
