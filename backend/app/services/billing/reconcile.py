"""
Reconciliation: find payment orders that were paid at Razorpay but never activated here, and activate them.

Why it exists: activation normally happens twice over (browser callback AND webhook). If both are lost — the tab
was closed, the webhook secret was wrong, an outage — the customer has paid and has no plan. Every 15 minutes
(Postgres advisory lock, one instance per tick) this looks at orders still `created`/`attempted` after
STUCK_AFTER and asks Razorpay what really happened:

  * order is paid  -> find its captured payment and run the normal `activate_from_payment` (same verification,
                      idempotency and invoice as any other activation), then alert the operator — a reconcile
                      activation means a callback path is broken.
  * unpaid and older than EXPIRE_AFTER -> mark the order `failed` ("expired: …"). A later payment on that order
                      would still activate (activation accepts failed orders), so nothing is lost.
  * otherwise      -> leave alone (the customer may still be paying).

Orders of the other mode (test vs live) are skipped: this process cannot query them. dry_run=True only counts what
WOULD be activated (used by /health/billing?verify=true).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.sellertalk24_billing import PaymentOrder, PaymentOrderStatus
from app.services.billing import ops
from app.services.billing.activation import ActivationError, activate_from_payment
from app.services.billing.razorpay_client import BillingConfigError, BillingGateway, BillingGatewayError
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=30)
EXPIRE_AFTER = timedelta(hours=24)
LOOKBACK = timedelta(days=3)           # older unpaid orders were already expired by an earlier tick
BATCH = 200
ADVISORY_LOCK_KEY = 0x53543234_0002    # sibling of maintenance's ..._0001


@dataclass
class ReconcileReport:
    """What one reconcile pass did."""

    checked: int = 0
    activated: int = 0               # (dry run: would activate)
    expired: int = 0
    skipped_mode: int = 0
    errors: int = 0
    order_ids: list[int] = field(default_factory=list)   # orders activated / that would be


async def stuck_orders(db: AsyncSession, now: datetime) -> list:
    """Rows (id, razorpay_order_id, created_at, mode) of orders unpaid for over STUCK_AFTER, oldest first."""
    return list((
        await db.execute(
            select(PaymentOrder.id, PaymentOrder.razorpay_order_id, PaymentOrder.created_at, PaymentOrder.mode)
            .where(
                PaymentOrder.status.in_((PaymentOrderStatus.CREATED, PaymentOrderStatus.ATTEMPTED)),
                PaymentOrder.created_at < now - STUCK_AFTER,
                PaymentOrder.created_at > now - LOOKBACK,
            )
            .order_by(PaymentOrder.id).limit(BATCH)
        )
    ).all())


async def _expire(db: AsyncSession, order_id: int) -> bool:
    """Mark a still-unpaid order failed ('expired'). Re-checks under the row lock; True if it changed."""
    order = (
        await db.execute(
            select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if order.status not in (PaymentOrderStatus.CREATED, PaymentOrderStatus.ATTEMPTED):
        await db.commit()
        return False
    order.status = PaymentOrderStatus.FAILED
    order.failure_reason = "expired: no payment within 24 hours of checkout"
    await db.commit()
    return True


async def reconcile_orders(
    db: AsyncSession, gateway: BillingGateway, now: datetime | None = None, *, dry_run: bool = False
) -> ReconcileReport:
    """One reconcile pass over the stuck orders (see the module docstring). Per-order failures are isolated."""
    now = now or now_utc()
    report = ReconcileReport()
    for row in await stuck_orders(db, now):
        if row.mode != gateway.mode:
            report.skipped_mode += 1
            continue
        report.checked += 1
        try:
            remote = await gateway.fetch_order(row.razorpay_order_id)
            paid = remote.get("status") == "paid" or (
                isinstance(remote.get("amount_paid"), int) and remote.get("amount_paid", 0) > 0
                and remote.get("amount_paid") >= remote.get("amount", 1 << 60)
            )
            if paid:
                payments = await gateway.fetch_order_payments(row.razorpay_order_id)
                captured = next((p for p in payments if p.get("status") == "captured"), None)
                if captured is None:
                    logger.warning("billing reconcile: order %s is paid at Razorpay but has no captured payment", row.id)
                    report.errors += 1
                    continue
                if dry_run:
                    report.activated += 1
                    report.order_ids.append(row.id)
                    continue
                await activate_from_payment(
                    db, gateway, razorpay_order_id=row.razorpay_order_id,
                    razorpay_payment_id=captured["id"], source="reconcile", now=now,
                )
                report.activated += 1
                report.order_ids.append(row.id)
                logger.error("billing reconcile: ACTIVATED order %s from Razorpay's records (payment %s)", row.id, captured["id"])
                await ops.send_admin_alert(
                    db, "reconcile_activation", str(row.id), f"Reconcile activated payment order {row.id}",
                    f"Payment order {row.id} ({row.razorpay_order_id}) was paid at Razorpay but neither the checkout "
                    "callback nor the webhook had activated it for over 30 minutes; the reconcile job activated it "
                    f"(payment {captured['id']}). Check webhook delivery in the Razorpay dashboard and "
                    "GET /health/billing — a callback path is broken.",
                )
            elif not dry_run and now - row.created_at > EXPIRE_AFTER:
                if await _expire(db, row.id):
                    report.expired += 1
        except (BillingGatewayError, ActivationError) as exc:
            report.errors += 1
            logger.warning("billing reconcile: order %s not reconciled: %s", row.id, exc)
        except Exception:
            report.errors += 1
            logger.exception("billing reconcile: unexpected failure on order %s", row.id)
    return report


async def run_reconcile_job(
    factory: async_sessionmaker[AsyncSession] | None = None, gateway: BillingGateway | None = None,
    now: datetime | None = None,
) -> ReconcileReport | None:
    """
    Scheduler entry point: one pass under the advisory lock. Returns the report, or None when another instance
    holds the lock or billing is misconfigured (stamped as such). Records the run in billing_job_runs.
    """
    if factory is None:
        from app.db import _get_session_factory

        factory = _get_session_factory()
    engine = factory.kw["bind"]
    started = now_utc()
    async with engine.connect() as lock_conn:
        if not await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}):
            logger.info("billing reconcile: another instance holds the lock — skipping this tick")
            return None
        try:
            async with factory() as db:
                if gateway is None:
                    try:
                        from app.config import get_settings
                        from app.services.billing.razorpay_client import get_billing_gateway

                        gateway = get_billing_gateway(get_settings())
                    except BillingConfigError as exc:
                        logger.error("billing reconcile: billing is misconfigured, nothing reconciled: %s", exc)
                        await ops.stamp_job(db, "reconcile", "error", started_at=started, error=str(exc)[:300])
                        return None
                try:
                    report = await reconcile_orders(db, gateway, now)
                except Exception as exc:
                    await ops.stamp_job(db, "reconcile", "error", started_at=started, error=f"{type(exc).__name__}: {exc}"[:300])
                    raise
                await ops.prune_ops_events(db)
                await ops.stamp_job(
                    db, "reconcile", "ok", started_at=started, checked=report.checked, activated=report.activated,
                    expired=report.expired, skipped_mode=report.skipped_mode, errors=report.errors,
                )
            logger.info(
                "billing reconcile: checked=%d activated=%d expired=%d skipped_mode=%d errors=%d",
                report.checked, report.activated, report.expired, report.skipped_mode, report.errors,
            )
            return report
        finally:
            await lock_conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await lock_conn.rollback()
