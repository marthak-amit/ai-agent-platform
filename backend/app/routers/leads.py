"""
Leads API router.

Most endpoints here are protected (require JWT) and provide the React
dashboard with lead data filtered by status. POST /leads/public is the one
exception — it is unauthenticated and accepts demo-request submissions from
the public marketing site, so it is registered on its own router with no
dependencies.
"""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.lead import Lead
from app.routers.auth import get_owner_client as get_current_client
from app.services import outbound

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/leads",
    tags=["leads"],
    dependencies=[Depends(get_current_client)],
)

# Separate, unauthenticated router for the public demo-request form — must
# NOT inherit the get_current_client dependency above.
public_router = APIRouter(prefix="/leads", tags=["leads"])


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


# ─────────────────────────────────────────────────────────────────────────────
# Public, unauthenticated demo-request endpoint (marketing site)
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# In-process IP-based rate limiter, same pattern as webhook.py's per-phone
# limiter. Fine for a single Railway instance; see CLAUDE.md Redis upgrade
# note if this ever needs to span multiple workers/instances.
_demo_lead_store: dict[str, list[datetime]] = defaultdict(list)
_demo_lead_lock = asyncio.Lock()
_DEMO_LEAD_LIMIT = 5
_DEMO_LEAD_WINDOW_SECONDS = 3600


async def _notify_internal_team(body: "PublicDemoLeadRequest") -> None:
    """
    Best-effort WhatsApp ping to the AgentlyAI team about a new demo lead.

    Silently no-ops if INTERNAL_LEAD_NOTIFY_NUMBER is unset. Never raises —
    a notification failure must not affect the lead write or the API response.
    """
    notify_number = get_settings().internal_lead_notify_number
    if not notify_number:
        return

    contact = body.whatsapp_number or body.phone or "—"
    volume = body.monthly_order_volume or "—"
    message_snippet = (body.message or "—")[:200]

    text = (
        f"New demo lead: {body.business_name} | {body.email} | {contact} | "
        f"volume: {volume} | msg: {message_snippet}"
    )

    try:
        await outbound.send_owner_text(notify_number, text)
    except Exception as exc:
        logger.error("Failed to send internal demo-lead WhatsApp notification: %s", exc)


async def _is_demo_lead_rate_limited(client_ip: str) -> bool:
    """Return True if *client_ip* has exceeded the public demo-form submission cap."""
    async with _demo_lead_lock:
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=_DEMO_LEAD_WINDOW_SECONDS)
        _demo_lead_store[client_ip] = [
            ts for ts in _demo_lead_store[client_ip] if ts > window_start
        ]
        if len(_demo_lead_store[client_ip]) >= _DEMO_LEAD_LIMIT:
            return True
        _demo_lead_store[client_ip].append(now)
        return False


class PublicDemoLeadRequest(BaseModel):
    """Request body for the public marketing-site 'Book a Demo' form."""

    business_name: str
    email: str
    phone: Optional[str] = None
    whatsapp_number: Optional[str] = None
    monthly_order_volume: Optional[str] = None
    message: Optional[str] = None
    # Hidden honeypot field — real visitors never see or fill this. Any
    # non-empty value here means the submission is from a bot.
    company_website: Optional[str] = None

    @field_validator("business_name")
    @classmethod
    def _business_name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("business_name is required.")
        return v.strip()

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or not _EMAIL_RE.match(v):
            raise ValueError("A valid email is required.")
        return v


class PublicDemoLeadResponse(BaseModel):
    """Response for a successful demo-request submission. Leaks no internal IDs."""

    ok: bool = True


@public_router.post(
    "/public",
    response_model=PublicDemoLeadResponse,
    status_code=http_status.HTTP_200_OK,
)
async def submit_public_demo_lead(
    body: PublicDemoLeadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicDemoLeadResponse:
    """
    Accept an unauthenticated 'Book a Demo' submission from the marketing site.

    Args:
        body:    Demo-request form fields (see PublicDemoLeadRequest).
        request: Used only to read the caller's IP for rate limiting.
        db:      Injected async DB session.

    Returns:
        {"ok": true} on success. Always 200 on validation pass — never leaks
        internal lead IDs to an anonymous caller.

    Raises:
        HTTPException 429: If the caller's IP has exceeded the submission cap.
    """
    from app.models.demo_lead import DemoLead

    # Honeypot — bots fill every field including ones hidden from real users.
    if body.company_website:
        logger.info("Rejected demo-lead submission: honeypot field filled.")
        return PublicDemoLeadResponse(ok=True)  # don't tip off the bot

    client_ip = request.client.host if request.client else "unknown"
    if await _is_demo_lead_rate_limited(client_ip):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many demo requests from this network. Please try again later.",
        )

    demo_lead = DemoLead(
        business_name=body.business_name,
        email=body.email,
        phone=body.phone,
        whatsapp_number=body.whatsapp_number,
        monthly_order_volume=body.monthly_order_volume,
        message=body.message,
        source="marketing_site",
    )
    db.add(demo_lead)
    await db.commit()

    logger.info("New demo-lead request received from %s (%s).", body.business_name, body.email)
    await _notify_internal_team(body)
    return PublicDemoLeadResponse(ok=True)
