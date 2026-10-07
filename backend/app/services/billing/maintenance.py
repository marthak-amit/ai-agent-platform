"""
Billing maintenance job (every 15 minutes), safe to run on every instance at once.

`run_maintenance(db, now)` does the work; `run_maintenance_job()` is what the scheduler calls: it
first takes a Postgres advisory lock (pg_try_advisory_lock) on a dedicated connection, so with
several workers/instances exactly one runs a given tick and the rest return immediately. The lock
is connection-scoped, so it is released automatically if the process dies mid-run.

Per tick:
  1. Lapsed periods -> 'expired'; a queued (stacked) renewal that has started becomes 'active'
     (subscriptions.roll_forward — the same code the API uses lazily, so the job is only a
     safety net that makes sure state converges even for clients nobody is looking at).
  2. Expiry reminders: one alert at <=3 days and one at <=1 day before an active period ends
     (skipped when a renewal is already queued).
  3. Grace / expired alerts for clients with no running period.
  4. Hourly, while legacy counting is still on: log legacy-vs-new conversation numbers to compare.

Every client is processed in its own transaction; one failing client never stops the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import BillingPlan, ClientSubscription, SubscriptionStatus
from app.services.billing import alerts, emails, ops
from app.services.billing.entitlement import EntitlementState, get_entitlement
from app.services.billing.subscriptions import get_plan_by_id, now_utc, roll_forward

logger = logging.getLogger(__name__)

#: Arbitrary constant namespace for the advisory lock ("ST24" + job id).
ADVISORY_LOCK_KEY = 0x53543234_0001


@dataclass
class MaintenanceReport:
    """What one tick did (for logs and tests)."""

    expired: int = 0
    promoted: int = 0
    reminders: int = 0
    grace_alerts: int = 0
    errors: int = 0
    clients: list[int] = field(default_factory=list)


async def _clients_needing_roll_forward(db: AsyncSession, now: datetime) -> list[int]:
    """Clients with a lapsed 'active' period, or a started 'pending' period but nothing active."""
    lapsed = select(ClientSubscription.client_id).where(
        ClientSubscription.status == SubscriptionStatus.ACTIVE, ClientSubscription.current_period_end <= now
    )
    # A pending period whose start has passed (its predecessor ended). roll_forward is idempotent,
    # so being generous here is harmless.
    started_pending = select(ClientSubscription.client_id).where(
        ClientSubscription.status == SubscriptionStatus.PENDING,
        ClientSubscription.current_period_start <= now,
    )
    ids = set((await db.execute(lapsed)).scalars().all())
    ids.update((await db.execute(started_pending)).scalars().all())
    return sorted(ids)


async def _roll_client(db: AsyncSession, client_id: int, now: datetime, report: MaintenanceReport) -> None:
    """Apply roll_forward for one client and count what changed."""
    before = {
        s.id: s.status
        for s in (await db.execute(select(ClientSubscription).where(ClientSubscription.client_id == client_id))).scalars()
    }
    await roll_forward(db, client_id, now)
    await db.commit()
    db.expire_all()
    for sub in (await db.execute(select(ClientSubscription).where(ClientSubscription.client_id == client_id))).scalars():
        old = before.get(sub.id)
        if old == SubscriptionStatus.ACTIVE and sub.status == SubscriptionStatus.EXPIRED:
            report.expired += 1
        elif old == SubscriptionStatus.PENDING and sub.status == SubscriptionStatus.ACTIVE:
            report.promoted += 1


async def _reminders(db: AsyncSession, now: datetime, report: MaintenanceReport) -> None:
    """Raise the 3-day / 1-day expiry reminders for active periods with no renewal queued."""
    queued = aliased(ClientSubscription)
    rows = (
        await db.execute(
            select(ClientSubscription.id, ClientSubscription.client_id, ClientSubscription.plan_id,
                   ClientSubscription.current_period_end)
            .where(
                ClientSubscription.status == SubscriptionStatus.ACTIVE,
                ClientSubscription.current_period_end > now,
                ClientSubscription.current_period_end <= now + timedelta(days=3),
                ~exists().where(
                    queued.client_id == ClientSubscription.client_id,
                    queued.status == SubscriptionStatus.PENDING,
                ),
            )
        )
    ).all()
    to_email: list[tuple[int, str, dict]] = []
    for row in rows:
        left = row.current_period_end - now
        kind, days = ("expiring_1d", 1) if left <= timedelta(days=1) else ("expiring_3d", 3)
        plan = await get_plan_by_id(db, row.plan_id)
        params = {"days": days, "plan": plan.name if plan else "your plan", "end": f"{row.current_period_end:%d %b %Y}"}
        created = await alerts.raise_alert(
            db, row.client_id, kind, f"{kind}:{row.id}", subscription_id=row.id, **params,
        )
        report.reminders += int(created)
        if created:
            to_email.append((row.client_id, kind, params))
    await db.commit()
    # Email only after the alert rows are durable, and only for newly created alerts (= once per period).
    for client_id, kind, params in to_email:
        await emails.notify_alert(db, client_id, kind, params)


async def _grace_alerts(db: AsyncSession, now: datetime, report: MaintenanceReport) -> None:
    """For clients with no running period raise 'grace_started' (in grace) or 'expired' (grace over), once per lapse."""
    clients = (
        await db.execute(select(Client).where(Client.is_active.is_(True), Client.billing_exempt.is_(False)))
    ).scalars().all()
    for client in clients:
        try:
            ent = await get_entitlement(db, client, now)
            if ent.state is EntitlementState.GRACE:
                kind = "grace_started"
                params = {"grace_ends": f"{ent.grace_ends_at:%d %b %Y}", "grace_days": get_settings().grace_days}
                created = await alerts.raise_alert(db, client.id, kind, f"grace:{ent.anchor_key}", **params)
            elif ent.state is EntitlementState.EXPIRED:
                kind, params = "expired", {}
                created = await alerts.raise_alert(db, client.id, kind, f"expired:{ent.anchor_key}")
            else:
                continue
            report.grace_alerts += int(created)
            client_id = client.id
            await db.commit()
            if created:
                await emails.notify_alert(db, client_id, kind, params)
        except Exception:
            await db.rollback()
            report.errors += 1
            logger.exception("billing maintenance: grace alerts failed for client %s", getattr(client, "id", "?"))


async def _log_count_comparison(db: AsyncSession, now: datetime) -> None:
    """While legacy counting is on, log legacy month count vs new period count per client (hourly)."""
    from app.models.client_monthly_usage import ClientMonthlyUsage

    period = f"{now:%Y-%m}"
    rows = (
        await db.execute(
            select(ClientSubscription.client_id, ClientSubscription.conversations_used, BillingPlan.code)
            .join(BillingPlan, BillingPlan.id == ClientSubscription.plan_id)
            .where(ClientSubscription.status == SubscriptionStatus.ACTIVE)
        )
    ).all()
    for client_id, new_used, plan_code in rows:
        legacy = await db.scalar(
            select(ClientMonthlyUsage.conv_count).where(
                ClientMonthlyUsage.client_id == client_id, ClientMonthlyUsage.period == period
            )
        )
        logger.info(
            "BILLING_COUNT_COMPARE client=%s plan=%s legacy_calendar_month=%s new_billing_period=%s",
            client_id, plan_code, legacy or 0, new_used,
        )


async def run_maintenance(db: AsyncSession, now: datetime | None = None) -> MaintenanceReport:
    """One maintenance tick (see the module docstring). Per-client failures are isolated."""
    now = now or now_utc()
    report = MaintenanceReport()

    for client_id in await _clients_needing_roll_forward(db, now):
        try:
            await _roll_client(db, client_id, now, report)
            report.clients.append(client_id)
        except Exception:
            await db.rollback()
            report.errors += 1
            logger.exception("billing maintenance: roll-forward failed for client %s", client_id)

    for step in (_reminders, _grace_alerts):
        try:
            await step(db, now, report)
        except Exception:
            await db.rollback()
            report.errors += 1
            logger.exception("billing maintenance: %s failed", step.__name__)

    if get_settings().billing_legacy_counting and now.minute < 15:
        try:
            await _log_count_comparison(db, now)
        except Exception:
            await db.rollback()
            logger.exception("billing maintenance: count comparison failed")
    return report


async def run_maintenance_job(
    factory: async_sessionmaker[AsyncSession] | None = None, now: datetime | None = None
) -> MaintenanceReport | None:
    """
    Scheduler entry point: run one tick under the advisory lock.

    Returns the report, or None when another instance holds the lock (this tick is skipped).
    """
    if factory is None:
        from app.db import _get_session_factory

        factory = _get_session_factory()
    engine = factory.kw["bind"]

    async with engine.connect() as lock_conn:
        got = await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        if not got:
            logger.info("billing maintenance: another instance holds the lock — skipping this tick")
            return None
        started = now_utc()
        try:
            async with factory() as db:
                try:
                    report = await run_maintenance(db, now)
                except Exception as exc:
                    await ops.stamp_job(db, "maintenance", "error", started_at=started, error=f"{type(exc).__name__}: {exc}"[:300])
                    raise
                await ops.stamp_job(
                    db, "maintenance", "ok" if report.errors == 0 else "errors", started_at=started,
                    expired=report.expired, promoted=report.promoted, reminders=report.reminders,
                    grace_alerts=report.grace_alerts, errors=report.errors,
                )
            logger.info(
                "billing maintenance: expired=%d promoted=%d reminders=%d grace_alerts=%d errors=%d",
                report.expired, report.promoted, report.reminders, report.grace_alerts, report.errors,
            )
            return report
        finally:
            await lock_conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await lock_conn.rollback()
