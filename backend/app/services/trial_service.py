"""
Free trial: starts at "Go live" (never at signup), ends at whichever limit is hit first, then grace, then expiry.

Rules (config: TRIAL_DAYS=7, TRIAL_CONVERSATION_LIMIT=100, GRACE_DAYS=3):
  * A trial starts when the owner presses "Go live" (`start_trial`), which sets `trial_started_at` / `trial_ends_at`.
    Before that the account is in status 'setup': unlimited simulator, no expiry, no countdown.
  * The trial ends after TRIAL_DAYS days or TRIAL_CONVERSATION_LIMIT counted conversations, whichever comes first.
    Conversations are the same 24h-window rows the paid plans count (`conversation_usage_log`, written with no
    subscription during a trial). Simulator chats are never counted — they never reach the usage counter.
  * After the trial the normal lapse rules apply: GRACE_DAYS of grace, then EXPIRED (restricted only when
    SELLERTALK24_BILLING_ENFORCE is on, exactly like every other expired account).
  * One trial per WhatsApp phone_number_id, ever (`trial_claims`, UNIQUE): disconnecting/reconnecting a number or
    opening a second account on it does not earn another trial. Such an account must subscribe to go live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sellertalk24_billing import ConversationUsageLog
from app.models.trial_claim import TrialClaim

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrialStanding:
    """Where a client's trial stands at a moment in time."""

    active: bool
    used: int
    limit: int
    ends_at: datetime | None
    ended_at: datetime | None = None      # when the trial actually stopped (time or conversation cap); None while active
    reason: str = ""                      # "" while active | "time" | "conversations"


def trial_days() -> int:
    """Trial length in days (TRIAL_DAYS)."""
    return int(get_settings().trial_days)


def trial_conversation_limit() -> int:
    """Conversations included in the trial (TRIAL_CONVERSATION_LIMIT)."""
    return int(get_settings().trial_conversation_limit)


def _trial_usage_query(client_id: int, since: datetime):
    """Usage-log rows counted against the trial: no subscription (so not a paid period) since the trial began."""
    return (
        ConversationUsageLog.client_id == client_id,
        ConversationUsageLog.subscription_id.is_(None),
        ConversationUsageLog.created_at >= since,
    )


async def trial_standing(db: AsyncSession, client, now: datetime) -> TrialStanding:
    """
    Compute the trial standing for *client* (status 'trial' with `trial_started_at` set).

    One indexed COUNT; a second query only when the conversation cap has just been reached (to find *when*, which
    anchors the grace period).
    """
    limit = trial_conversation_limit()
    started = client.trial_started_at
    ends_at = client.trial_ends_at
    if started is None:
        return TrialStanding(active=False, used=0, limit=limit, ends_at=ends_at, ended_at=ends_at, reason="time")
    used = int(await db.scalar(select(func.count()).select_from(ConversationUsageLog).where(*_trial_usage_query(client.id, started))) or 0)
    time_over = ends_at is not None and now >= ends_at
    cap_hit_at = None
    if used >= limit:
        cap_hit_at = await db.scalar(
            select(ConversationUsageLog.created_at)
            .where(*_trial_usage_query(client.id, started))
            .order_by(ConversationUsageLog.created_at.asc(), ConversationUsageLog.id.asc())
            .offset(limit - 1)
            .limit(1)
        )
    if cap_hit_at is not None and (ends_at is None or cap_hit_at <= ends_at):
        return TrialStanding(False, used, limit, ends_at, ended_at=cap_hit_at, reason="conversations")
    if time_over:
        return TrialStanding(False, used, limit, ends_at, ended_at=ends_at, reason="time")
    return TrialStanding(True, used, limit, ends_at)


async def claim_phone_number(db: AsyncSession, client_id: int, phone_number_id: str) -> bool:
    """
    Try to claim the one trial of *phone_number_id* for *client_id*.

    Returns True when the claim is (now or already) this client's, False when another account owns it. Atomic:
    the UNIQUE constraint decides, so two simultaneous go-lives cannot both win.
    """
    inserted = await db.scalar(
        pg_insert(TrialClaim)
        .values(phone_number_id=phone_number_id, client_id=client_id)
        .on_conflict_do_nothing(constraint="uq_trial_claims_phone_number_id")
        .returning(TrialClaim.id)
    )
    if inserted is not None:
        return True
    owner = await db.scalar(select(TrialClaim.client_id).where(TrialClaim.phone_number_id == phone_number_id))
    return owner == client_id


def start_trial(client, now: datetime) -> None:
    """Stamp the trial window on *client* and move it to status 'trial' (caller commits)."""
    client.trial_started_at = now
    client.trial_ends_at = now + timedelta(days=trial_days())
    client.account_status = "trial"
