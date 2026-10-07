"""
Billing operations plumbing: ops events, admin alerts, job-run stamps.

  record_ops_event(..)     insert a `billing_ops_events` row (optionally with a UNIQUE dedupe key). Does not commit.
  send_admin_alert(..)     "tell the operator once per incident": dedupe via the ops table (so it holds across
                           workers), then log at ERROR and email BILLING_ALERT_EMAIL through the normal email
                           provider (EMAIL_PROVIDER=console just logs it). Never raises.
  record_bad_signature(..) log a rejected webhook and alert when more than BURST_THRESHOLD arrive in BURST_WINDOW.
  stamp_job(..)            upsert `billing_job_runs` for /health/billing.

Nothing in here may break the caller: a payment activation, a webhook or a scheduler tick must never fail because
an alert could not be stored or sent. Callers hold no open transaction state we could damage — these helpers use a
SAVEPOINT and then `commit()`; they never `rollback()` (that would expire the caller's ORM objects).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sellertalk24_billing import BillingJobRun, BillingOpsEvent
from app.services import email_service
from app.services.billing.subscriptions import now_utc
from app.services.email_service import EmailMessage

logger = logging.getLogger(__name__)

BURST_WINDOW = timedelta(minutes=10)
BURST_THRESHOLD = 5            # alert when MORE than this many bad signatures land inside the window
MAX_SIGNATURE_ROWS = 500       # stop storing rows beyond this per window (an attacker must not fill the table)
OPS_RETENTION = timedelta(days=14)


async def record_ops_event(db: AsyncSession, kind: str, *, dedupe_key: str | None = None, **detail: Any) -> bool:
    """Insert an ops event; True if a row was created (False when *dedupe_key* already exists). Does not commit."""
    stmt = pg_insert(BillingOpsEvent).values(kind=kind, dedupe_key=dedupe_key, detail=detail)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_sellertalk24_billing_ops_events_dedupe")
    return (await db.scalar(stmt.returning(BillingOpsEvent.id))) is not None


async def send_admin_alert(db: AsyncSession, kind: str, dedupe_key: str, subject: str, body: str) -> bool:
    """
    Alert the operator once per (kind, dedupe_key). Returns True when this call was the first and the email was
    accepted by the provider (False for a repeat, a missing BILLING_ALERT_EMAIL, or any failure — all logged).
    """
    try:
        async with db.begin_nested():
            first = await record_ops_event(db, "admin_alert", dedupe_key=f"{kind}:{dedupe_key}", alert_kind=kind, subject=subject)
        await db.commit()
    except Exception:
        logger.exception("billing: could not record admin alert %s/%s", kind, dedupe_key)
        first = True   # better a possible duplicate than a lost alert
    if not first:
        return False
    logger.error("BILLING ADMIN ALERT [%s] %s — %s", kind, subject, body)
    to = get_settings().billing_alert_email.strip()
    if not to:
        logger.warning("BILLING_ALERT_EMAIL is not set — admin alert [%s] was only logged", kind)
        return False
    return await email_service.send_email(
        EmailMessage(to=to, subject=f"[SellerTalk24 billing] {subject}", text=body)
    )


async def record_bad_signature(db: AsyncSession, now: datetime | None = None) -> int:
    """
    Log a rejected webhook delivery; alert once per window when the burst threshold is exceeded.
    Returns the number of bad signatures seen in the window (including this one). Never raises.
    """
    now = now or now_utc()
    try:
        since = now - BURST_WINDOW
        recent = int(await db.scalar(
            select(func.count()).select_from(BillingOpsEvent).where(
                BillingOpsEvent.kind == "webhook_bad_signature", BillingOpsEvent.created_at >= since)
        ) or 0)
        if recent < MAX_SIGNATURE_ROWS:
            await record_ops_event(db, "webhook_bad_signature")
            await db.commit()
        count = recent + 1
    except Exception:
        logger.exception("billing: could not record a bad webhook signature")
        return 0
    if count > BURST_THRESHOLD:
        bucket = int(now.timestamp() // BURST_WINDOW.total_seconds())
        await send_admin_alert(
            db, "webhook_signature_burst", str(bucket),
            f"{count} webhook signature failures in {int(BURST_WINDOW.total_seconds() // 60)} minutes",
            f"/billing/webhook rejected {count} deliveries with an invalid signature in the last "
            f"{int(BURST_WINDOW.total_seconds() // 60)} minutes. Either RAZORPAY_WEBHOOK_SECRET does not match the "
            "Razorpay dashboard (all real payments will fail to confirm via webhook) or someone is probing the endpoint.",
        )
    return count


async def prune_ops_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete bad-signature rows older than OPS_RETENTION (admin-alert dedupe markers are kept). Commits."""
    from sqlalchemy import delete

    now = now or now_utc()
    result = await db.execute(
        delete(BillingOpsEvent).where(
            BillingOpsEvent.kind == "webhook_bad_signature", BillingOpsEvent.created_at < now - OPS_RETENTION)
    )
    await db.commit()
    return int(result.rowcount or 0)


async def stamp_job(
    db: AsyncSession, job: str, status: str, *, started_at: datetime | None = None, **detail: Any
) -> None:
    """Upsert the last-run row for *job* ('ok' / 'error' / 'skipped_locked'). Never raises; commits."""
    try:
        now = now_utc()
        values = dict(job=job, last_started_at=started_at or now, last_finished_at=now, last_status=status, detail=detail)
        await db.execute(
            pg_insert(BillingJobRun).values(**values).on_conflict_do_update(
                index_elements=["job"], set_={k: v for k, v in values.items() if k != "job"})
        )
        await db.commit()
    except Exception:
        logger.exception("billing: could not stamp job run %s", job)
