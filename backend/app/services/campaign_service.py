"""
Campaign service — personalisation, plan guards, and broadcast sending.

Sending runs entirely in-process, using asyncio.sleep(0.1) between messages
to respect Meta's ~1 000 messages/min rate limit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign
from app.models.campaign_recipient import CampaignRecipient
from app.services import whatsapp_service

logger = logging.getLogger(__name__)

# ── Plan limits ───────────────────────────────────────────────────────────────

_PLAN_LIMITS: dict[str, dict] = {
    "starter": {"allowed": False, "max_recipients": 0,   "monthly_campaigns": 0},
    "growth":  {"allowed": True,  "max_recipients": 500,  "monthly_campaigns": 1},
    "pro":     {"allowed": True,  "max_recipients": 99999, "monthly_campaigns": 99999},
}


def check_plan_allows_campaigns(plan_slug: str) -> None:
    """
    Raise ValueError if the plan does not allow campaigns.

    Args:
        plan_slug: Client's current plan slug.

    Raises:
        ValueError: Descriptive message for the HTTP layer to forward.
    """
    limits = _PLAN_LIMITS.get(plan_slug, _PLAN_LIMITS["starter"])
    if not limits["allowed"]:
        raise ValueError(
            "Broadcast campaigns require the Growth or Pro plan. "
            "Upgrade your plan to send campaigns."
        )


def check_recipient_limit(plan_slug: str, recipient_count: int) -> None:
    """
    Raise ValueError if recipient_count exceeds the plan's per-campaign cap.

    Args:
        plan_slug:       Client's current plan slug.
        recipient_count: Number of recipients to add.

    Raises:
        ValueError: If the count would exceed the plan's max_recipients.
    """
    limits = _PLAN_LIMITS.get(plan_slug, _PLAN_LIMITS["starter"])
    cap = limits["max_recipients"]
    if cap >= 99999:
        return  # Pro — unlimited
    if recipient_count > cap:
        raise ValueError(
            f"Your plan allows a maximum of {cap} recipients per campaign. "
            f"You tried to add {recipient_count}."
        )


async def check_monthly_campaign_limit(
    plan_slug: str, client_id: int, db: AsyncSession
) -> None:
    """
    Raise ValueError if the client has already hit their monthly campaign quota.

    Args:
        plan_slug: Client's current plan slug.
        client_id: Client row ID.
        db:        Active async DB session.

    Raises:
        ValueError: If monthly quota is exhausted.
    """
    from sqlalchemy import func
    from datetime import date

    limits = _PLAN_LIMITS.get(plan_slug, _PLAN_LIMITS["starter"])
    monthly_cap = limits["monthly_campaigns"]
    if monthly_cap >= 99999:
        return  # Pro — unlimited

    today = date.today()
    month_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)

    result = await db.execute(
        select(func.count()).select_from(Campaign).where(
            Campaign.client_id == client_id,
            Campaign.status.in_(["running", "completed"]),
            Campaign.created_at >= month_start,
        )
    )
    used = result.scalar_one() or 0
    if used >= monthly_cap:
        raise ValueError(
            f"Your plan allows {monthly_cap} campaign(s) per month. "
            "Upgrade to Pro for unlimited campaigns."
        )


# ── Import helpers ────────────────────────────────────────────────────────────

async def import_recipients_from_conversations(
    campaign: Campaign, db: AsyncSession
) -> int:
    """
    Add all unique WhatsApp conversation phone numbers as pending recipients.

    Skips numbers that are already listed for this campaign.

    Args:
        campaign: Campaign ORM instance (must already be persisted).
        db:       Active async DB session.

    Returns:
        Number of new recipients added.
    """
    from app.models.conversation import Conversation

    existing_result = await db.execute(
        select(CampaignRecipient.phone_number).where(
            CampaignRecipient.campaign_id == campaign.id
        )
    )
    existing_phones: set[str] = {r[0] for r in existing_result.all()}

    conv_result = await db.execute(
        select(Conversation.phone_number).where(
            Conversation.channel == "whatsapp"
        ).distinct()
    )
    phones = [r[0] for r in conv_result.all() if r[0] not in existing_phones]

    for phone in phones:
        db.add(CampaignRecipient(
            campaign_id=campaign.id,
            phone_number=phone,
            status="pending",
        ))

    campaign.total_recipients = len(existing_phones) + len(phones)
    await db.commit()
    return len(phones)


# ── Core send logic ───────────────────────────────────────────────────────────

def _personalise(template: str, recipient: CampaignRecipient) -> str:
    """Replace {name} in template with the recipient's customer_name."""
    return template.replace("{name}", recipient.customer_name or "")


async def send_campaign(campaign_id: int, db: AsyncSession) -> None:
    """
    Execute a campaign: personalise and send to every pending recipient.

    Persists progress after each message so partial sends survive crashes.
    Respects Meta's rate limit via a 0.1 s delay between sends.

    Args:
        campaign_id: Campaign row ID.
        db:          Active async DB session.

    Raises:
        ValueError: If campaign is not found or not in a sendable state.
    """
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found.")
    if campaign.status not in ("draft", "scheduled"):
        raise ValueError(
            f"Campaign is '{campaign.status}'. Only draft/scheduled campaigns can be sent."
        )

    recipients_result = await db.execute(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "pending",
        )
    )
    recipients: Sequence[CampaignRecipient] = recipients_result.scalars().all()

    campaign.status = "running"
    campaign.started_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        "Campaign %d '%s' started — %d recipients.", campaign_id, campaign.name, len(recipients)
    )

    for recipient in recipients:
        try:
            message = _personalise(campaign.message_template, recipient)
            await asyncio.sleep(0.1)  # ~600 msg/min — safely under Meta's 1 000/min cap
            await whatsapp_service.send_text_message(
                to_phone_number=recipient.phone_number,
                message_text=message,
            )
            recipient.status = "sent"
            recipient.sent_at = datetime.now(timezone.utc)
            campaign.sent_count += 1
        except Exception as exc:
            logger.warning(
                "Campaign %d: failed to send to %s — %s",
                campaign_id, recipient.phone_number, exc,
            )
            recipient.status = "failed"
            recipient.error_message = str(exc)[:500]
            campaign.failed_count += 1

        await db.commit()

    campaign.status = "completed"
    campaign.completed_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        "Campaign %d completed — sent=%d failed=%d.",
        campaign_id, campaign.sent_count, campaign.failed_count,
    )
