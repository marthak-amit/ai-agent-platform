"""
Conversation counting for SellerTalk24 plans.

A "conversation" is Meta's: one 24-hour window per (client, channel, customer), opened by the
customer's first inbound message when no window is open. Each window is counted ONCE toward the
active subscription period.

  record_conversation(...)       low level — insert the window row (ON CONFLICT DO NOTHING) and,
                                 only if the row was really inserted, `used = used + 1` in SQL
                                 (atomic; no read-modify-write). Raises alert rows when a usage
                                 threshold is crossed. All in one SAVEPOINT + one commit.
  track_inbound_conversation(..) what the message pipeline calls for every inbound message: one
                                 indexed SELECT for the customer's latest window; only a message
                                 that opens a new window goes on to record_conversation.

Never raises: any failure is logged and swallowed (the savepoint rolls back, so the caller's
session stays usable). Counting problems must never break the webhook / the order bot.
This is a SOFT cap: nothing here (or anywhere it leads) ever blocks a message.

`over_limit` means used > limit (strictly over); the 100% alert fires on reaching the limit (used >= limit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sellertalk24_billing import BillingPlan, ClientSubscription, ConversationUsageLog
from app.services.billing import alerts
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)               # Meta's customer-service / conversation window
THRESHOLD_PCTS = (80, 100, 120)
_DEDUPE_CONSTRAINT = "uq_sellertalk24_conversation_usage_window"


@dataclass(frozen=True)
class UsageRecord:
    """Result of record_conversation: was a new window counted, and against which period."""

    inserted: bool
    subscription_id: int | None = None
    used: int | None = None
    limit: int | None = None


def crossed_thresholds(used: int, limit: int) -> list[int]:
    """
    Thresholds (80/100/120 %) that the increment to *used* just crossed — exactly the ones where
    (used-1) was below and *used* is at/above, so each fires on a single increment.
    """
    if limit <= 0:
        return []
    return [p for p in THRESHOLD_PCTS if (used - 1) * 100 < limit * p <= used * 100]


async def _in_open_window(
    db: AsyncSession, client_id: int, channel: str, customer_key: str, now: datetime
) -> bool:
    """True if this customer's latest counted window (same client + channel) is still open at *now*."""
    latest = await db.scalar(
        select(func.max(ConversationUsageLog.window_started_at)).where(
            ConversationUsageLog.client_id == client_id,
            ConversationUsageLog.channel == channel,
            ConversationUsageLog.customer_key == customer_key,
        )
    )
    return latest is not None and now - latest < WINDOW


async def record_conversation(
    db: AsyncSession,
    client_id: int,
    channel: str,
    customer_key: str,
    window_started_at: datetime,
    now: datetime | None = None,
) -> UsageRecord:
    """
    Count the window (client, channel, customer_key, window_started_at) — idempotently.

    The usage-log INSERT is ON CONFLICT DO NOTHING on the unique window key; the period's
    `conversations_used` is incremented (in SQL) only when that INSERT inserted a row. The
    increment targets the client's running period (active, not past its end); with none —
    trial/grace/exempt — the window is logged with no subscription and nothing is incremented.

    Does not commit; runs inside the caller's transaction (wrap in begin_nested for isolation).
    """
    now = now or now_utc()
    log_id = await db.scalar(
        pg_insert(ConversationUsageLog)
        .values(client_id=client_id, channel=channel, customer_key=customer_key, window_started_at=window_started_at)
        .on_conflict_do_nothing(constraint=_DEDUPE_CONSTRAINT)
        .returning(ConversationUsageLog.id)
    )
    if log_id is None:
        return UsageRecord(inserted=False)

    row = (
        await db.execute(
            update(ClientSubscription)
            .where(
                ClientSubscription.client_id == client_id,
                ClientSubscription.status == "active",
                ClientSubscription.current_period_end > now,
            )
            .values(
                conversations_used=ClientSubscription.conversations_used + 1,
                over_limit=(ClientSubscription.conversations_used + 1) > ClientSubscription.conversation_limit,
            )
            .returning(ClientSubscription.id, ClientSubscription.conversations_used,
                       ClientSubscription.conversation_limit, ClientSubscription.plan_id)
        )
    ).first()
    if row is None:
        return UsageRecord(inserted=True)

    sub_id, used, limit, plan_id = row
    await db.execute(
        update(ConversationUsageLog).where(ConversationUsageLog.id == log_id).values(subscription_id=sub_id)
    )

    crossed = crossed_thresholds(used, limit)
    if crossed:
        plan_name = await db.scalar(select(BillingPlan.name).where(BillingPlan.id == plan_id))
        for pct in crossed:
            await alerts.raise_alert(
                db, client_id, f"usage_{pct}", f"usage_{pct}:{sub_id}", subscription_id=sub_id,
                pct=pct, used=used, limit=limit, plan=plan_name or "your plan",
            )
    return UsageRecord(inserted=True, subscription_id=sub_id, used=used, limit=limit)


async def track_inbound_conversation(
    db: AsyncSession, client_id: int, channel: str, customer_key: str, now: datetime | None = None
) -> UsageRecord | None:
    """
    Pipeline hook: count this inbound message's conversation if it opens a new 24h window.

    Cost for a message inside an open window: one indexed SELECT. A message that may open a window
    takes a per-customer advisory lock and re-checks (double-checked locking) before inserting.
    Never raises (returns None on any failure); returns the UsageRecord only when a window was
    actually counted.
    """
    now = now or now_utc()
    try:
        async with db.begin_nested():
            if await _in_open_window(db, client_id, channel, customer_key, now):
                return None                                  # the overwhelmingly common case: 1 SELECT
            # Possibly a new window. Serialize per customer so two simultaneous first messages
            # (different timestamps, so the unique key alone would not stop them) count once.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"st24:usage:{client_id}:{channel}:{customer_key}"},
            )
            if await _in_open_window(db, client_id, channel, customer_key, now):
                record = None                                # lost the race: the winner already counted it
            else:
                record = await record_conversation(db, client_id, channel, customer_key, now, now)
        await db.commit()                                    # also releases the advisory lock
        return record if record is not None and record.inserted else None
    except Exception:
        logger.exception(
            "billing: conversation counting failed (client=%s channel=%s) — ignored, bot unaffected",
            client_id, channel,
        )
        return None
