"""
Campaigns router — WhatsApp broadcast campaign management.

Endpoints:
- POST /campaigns                    : create a campaign (draft)
- GET  /campaigns                    : list all campaigns for the client
- GET  /campaigns/{id}               : get campaign with recipients
- POST /campaigns/{id}/add-recipients: add phone list or import from conversations
- POST /campaigns/{id}/send          : start sending immediately (background task)
- POST /campaigns/{id}/schedule      : schedule for a future datetime
- GET  /campaigns/{id}/stats         : delivery statistics
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.campaign import Campaign
from app.models.campaign_recipient import CampaignRecipient
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import campaign_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns", tags=["campaigns"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateCampaignRequest(BaseModel):
    """Body for POST /campaigns."""
    name: str = Field(..., min_length=1, max_length=200)
    message_template: str = Field(..., min_length=1, max_length=1024)


class RecipientIn(BaseModel):
    """A single recipient entry."""
    phone: str
    name: Optional[str] = None


class AddRecipientsRequest(BaseModel):
    """Body for POST /campaigns/{id}/add-recipients."""
    recipients: Optional[list[RecipientIn]] = None
    import_from_conversations: bool = False


class ScheduleRequest(BaseModel):
    """Body for POST /campaigns/{id}/schedule."""
    scheduled_at: datetime


class RecipientOut(BaseModel):
    """Per-recipient row returned in campaign detail."""
    id: int
    phone_number: str
    customer_name: Optional[str]
    status: str
    sent_at: Optional[datetime]
    error_message: Optional[str]

    model_config = {"from_attributes": True}


class CampaignOut(BaseModel):
    """Campaign summary (no recipients list)."""
    id: int
    name: str
    status: str
    message_template: str
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    total_recipients: int
    sent_count: int
    failed_count: int
    delivered_count: int

    model_config = {"from_attributes": True}


class CampaignDetailOut(CampaignOut):
    """Campaign with full recipient list."""
    recipients: list[RecipientOut]


class CampaignStatsOut(BaseModel):
    """Delivery statistics for a campaign."""
    total_recipients: int
    sent_count: int
    failed_count: int
    delivered_count: int
    pending_count: int
    delivery_rate: float


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_campaign_or_404(
    campaign_id: int, client_id: int, db: AsyncSession
) -> Campaign:
    """Fetch a campaign that belongs to the given client, or raise 404."""
    result = await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.client_id == client_id,
        )
    )
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return campaign


async def _run_campaign_background(campaign_id: int) -> None:
    """Background task: open a fresh DB session and execute the campaign send."""
    from app.db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        try:
            await campaign_service.send_campaign(campaign_id, db)
        except Exception as exc:
            logger.error("Background campaign %d failed: %s", campaign_id, exc)
            # Mark campaign as failed if the whole send loop crashes.
            result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            camp = result.scalar_one_or_none()
            if camp and camp.status == "running":
                camp.status = "failed"
                await db.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=CampaignOut)
async def create_campaign(
    body: CreateCampaignRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    """
    Create a new campaign in draft status.

    Enforces plan guard (Growth / Pro only).

    Args:
        body:           name and message_template.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Created CampaignOut.

    Raises:
        HTTPException 403: If the client's plan doesn't allow campaigns.
    """
    try:
        campaign_service.check_plan_allows_campaigns(current_client.plan_slug or "starter")
        await campaign_service.check_monthly_campaign_limit(
            current_client.plan_slug or "starter", current_client.id, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    campaign = Campaign(
        client_id=current_client.id,
        name=body.name,
        message_template=body.message_template,
        status="draft",
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> list[Campaign]:
    """
    List all campaigns for the authenticated client, newest first.

    Args:
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        List of CampaignOut objects.
    """
    result = await db.execute(
        select(Campaign)
        .where(Campaign.client_id == current_client.id)
        .order_by(Campaign.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
async def get_campaign(
    campaign_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    """
    Get a campaign with its full recipient list.

    Args:
        campaign_id:    Campaign row ID.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        CampaignDetailOut.

    Raises:
        HTTPException 404: If not found or not owned by client.
    """
    campaign = await _get_campaign_or_404(campaign_id, current_client.id, db)
    # Eagerly load recipients
    recipients_result = await db.execute(
        select(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
        .order_by(CampaignRecipient.id)
    )
    campaign.recipients = list(recipients_result.scalars().all())
    return campaign


@router.post("/{campaign_id}/add-recipients", status_code=status.HTTP_200_OK)
async def add_recipients(
    campaign_id: int,
    body: AddRecipientsRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Add recipients to a draft campaign.

    Either provide an explicit list in ``recipients``, or set
    ``import_from_conversations: true`` to import all WhatsApp conversation
    phone numbers automatically.

    Args:
        campaign_id:    Campaign row ID.
        body:           Recipients list or import flag.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        {"added": int, "total": int}

    Raises:
        HTTPException 400: If campaign is not in draft status.
        HTTPException 403: If recipient count exceeds plan limit.
        HTTPException 404: If campaign not found.
    """
    campaign = await _get_campaign_or_404(campaign_id, current_client.id, db)
    if campaign.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recipients can only be added to draft campaigns.",
        )

    added = 0

    if body.import_from_conversations:
        added = await campaign_service.import_recipients_from_conversations(campaign, db)
    elif body.recipients:
        try:
            campaign_service.check_recipient_limit(
                current_client.plan_slug or "starter", len(body.recipients)
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        # Deduplicate against existing recipients
        existing_result = await db.execute(
            select(CampaignRecipient.phone_number).where(
                CampaignRecipient.campaign_id == campaign_id
            )
        )
        existing_phones: set[str] = {r[0] for r in existing_result.all()}

        for r in body.recipients:
            if r.phone not in existing_phones:
                db.add(CampaignRecipient(
                    campaign_id=campaign_id,
                    phone_number=r.phone,
                    customer_name=r.name,
                    status="pending",
                ))
                existing_phones.add(r.phone)
                added += 1

        campaign.total_recipients = len(existing_phones)
        await db.commit()

    return {"added": added, "total": campaign.total_recipients}


@router.post("/{campaign_id}/send", status_code=status.HTTP_202_ACCEPTED)
async def send_campaign(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Start sending a campaign immediately in a background task.

    Args:
        campaign_id:    Campaign row ID.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        {"status": "queued", "campaign_id": int}

    Raises:
        HTTPException 400: If campaign is not in draft/scheduled status or has no recipients.
        HTTPException 404: If campaign not found.
    """
    campaign = await _get_campaign_or_404(campaign_id, current_client.id, db)
    if campaign.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Campaign is '{campaign.status}' — only draft or scheduled campaigns can be sent.",
        )
    if campaign.total_recipients == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add recipients before sending.",
        )

    background_tasks.add_task(_run_campaign_background, campaign_id)
    return {"status": "queued", "campaign_id": campaign_id}


@router.post("/{campaign_id}/schedule", status_code=status.HTTP_200_OK)
async def schedule_campaign(
    campaign_id: int,
    body: ScheduleRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Schedule a draft campaign for a specific future datetime.

    Args:
        campaign_id:    Campaign row ID.
        body:           scheduled_at datetime (must be in the future).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        {"status": "scheduled", "scheduled_at": str}

    Raises:
        HTTPException 400: If datetime is in the past or campaign is not draft.
        HTTPException 404: If campaign not found.
    """
    campaign = await _get_campaign_or_404(campaign_id, current_client.id, db)
    if campaign.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only draft campaigns can be scheduled.",
        )

    scheduled = body.scheduled_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    if scheduled <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scheduled_at must be a future datetime.",
        )

    campaign.scheduled_at = scheduled
    campaign.status = "scheduled"
    await db.commit()

    return {"status": "scheduled", "scheduled_at": scheduled.isoformat()}


@router.get("/{campaign_id}/stats", response_model=CampaignStatsOut)
async def get_campaign_stats(
    campaign_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> CampaignStatsOut:
    """
    Return delivery statistics for a campaign.

    Args:
        campaign_id:    Campaign row ID.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        CampaignStatsOut with counts and delivery_rate %.

    Raises:
        HTTPException 404: If campaign not found.
    """
    campaign = await _get_campaign_or_404(campaign_id, current_client.id, db)
    pending = campaign.total_recipients - campaign.sent_count - campaign.failed_count
    delivery_rate = (
        round(campaign.sent_count / campaign.total_recipients * 100, 1)
        if campaign.total_recipients > 0
        else 0.0
    )
    return CampaignStatsOut(
        total_recipients=campaign.total_recipients,
        sent_count=campaign.sent_count,
        failed_count=campaign.failed_count,
        delivered_count=campaign.delivered_count,
        pending_count=max(pending, 0),
        delivery_rate=delivery_rate,
    )
