"""
GET /health/billing — operator view of billing health (X-Admin-Key, same auth as /admin/*).

Separate from the public /health (which Railway polls and which must stay cheap and secret-free).

Reports: last webhook received, webhook errors and amount mismatches in the last 24 h, recent bad-signature
count, orders stuck in created/attempted for over 30 minutes, and when the maintenance and reconcile jobs last ran.
`?verify=true` additionally asks Razorpay (read-only) how many of the stuck orders are in fact PAID — the
reconcile check, without activating anything. `status` is "degraded" with the reasons in `problems` whenever
something here needs a human.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.models.sellertalk24_billing import BillingJobRun, BillingOpsEvent, PaymentEvent
from app.routers import billing as billing_router
from app.routers.admin import require_admin
from app.schemas.sellertalk24_billing import BillingHealthOut, JobRunOut
from app.services.billing import ops, reconcile
from app.services.billing.razorpay_client import BillingConfigError, get_billing_gateway, mode_for_settings
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"], dependencies=[Depends(require_admin)])

JOB_OVERDUE_AFTER = timedelta(minutes=45)

#: payment_events.error prefixes that are informational, not faults (a stale failure for a paid order, etc.).
BENIGN_ERROR_PREFIXES = (
    "payment.failed for an already paid order",
    "order_not_found:",
    "unknown payment for refund",
    "refund for an order that was never activated",
    "refund for an already refunded order",
    "refund ",   # "refund <id> exceeds what is left to refund…"
)


def _job(row: BillingJobRun | None, now) -> JobRunOut:
    """BillingJobRun -> JobRunOut (with the overdue flag)."""
    if row is None or row.last_finished_at is None:
        return JobRunOut(status="never", overdue=True)
    return JobRunOut(
        last_run_at=row.last_finished_at, status=row.last_status, detail=dict(row.detail or {}),
        overdue=now - row.last_finished_at > JOB_OVERDUE_AFTER,
    )


@router.get("/billing", response_model=BillingHealthOut)
async def billing_health(
    settings: Annotated[Settings, Depends(billing_router.get_settings)],
    verify: bool = Query(False, description="also ask Razorpay which stuck orders are actually paid (read-only)"),
    db: AsyncSession = Depends(get_db),
) -> BillingHealthOut:
    """Billing health snapshot (see the module docstring)."""
    now = now_utc()
    day_ago = now - timedelta(hours=24)

    last_webhook = await db.scalar(select(func.max(PaymentEvent.created_at)))
    errors = (
        await db.execute(select(PaymentEvent.error).where(PaymentEvent.error.is_not(None), PaymentEvent.created_at >= day_ago))
    ).scalars().all()
    actionable = [e for e in errors if not e.startswith(BENIGN_ERROR_PREFIXES)]
    mismatches = int(await db.scalar(
        select(func.count()).select_from(BillingOpsEvent).where(
            BillingOpsEvent.kind == "amount_mismatch", BillingOpsEvent.created_at >= day_ago)
    ) or 0)
    bad_sigs = int(await db.scalar(
        select(func.count()).select_from(BillingOpsEvent).where(
            BillingOpsEvent.kind == "webhook_bad_signature", BillingOpsEvent.created_at >= now - ops.BURST_WINDOW)
    ) or 0)
    stuck = await reconcile.stuck_orders(db, now)
    jobs = {r.job: r for r in (await db.execute(select(BillingJobRun))).scalars().all()}

    stuck_paid = None
    if verify:
        try:
            gateway = get_billing_gateway(settings)
            stuck_paid = (await reconcile.reconcile_orders(db, gateway, now, dry_run=True)).activated
        except BillingConfigError:
            logger.warning("billing health: verify requested but billing is misconfigured")

    reconcile_run, maintenance_run = _job(jobs.get("reconcile"), now), _job(jobs.get("maintenance"), now)
    problems: list[str] = []
    if actionable:
        problems.append(f"{len(actionable)} webhook event(s) with errors in the last 24h (GET /admin/billing/payment-events?has_error=true)")
    if mismatches:
        problems.append(f"{mismatches} amount mismatch(es) in the last 24h")
    if bad_sigs > ops.BURST_THRESHOLD:
        problems.append(f"{bad_sigs} webhook signature failures in the last 10 minutes")
    if stuck_paid:
        problems.append(f"{stuck_paid} stuck order(s) are PAID at Razorpay and not yet activated")
    if reconcile_run.overdue:
        problems.append("reconcile job has not run in the last 45 minutes")
    if maintenance_run.overdue:
        problems.append("maintenance job has not run in the last 45 minutes")
    for name, run in (("reconcile", reconcile_run), ("maintenance", maintenance_run)):
        if run.status in ("error", "errors"):
            problems.append(f"{name} job's last run reported {run.status}")

    return BillingHealthOut(
        status="degraded" if problems else "ok", checked_at=now, mode=mode_for_settings(settings),
        mock=settings.billing_mock_enabled, last_webhook_received_at=last_webhook,
        webhook_errors_24h=len(errors), webhook_errors_actionable_24h=len(actionable),
        amount_mismatches_24h=mismatches, bad_signatures_10m=bad_sigs, stuck_orders=len(stuck),
        stuck_paid_at_razorpay=stuck_paid, reconcile=reconcile_run, maintenance=maintenance_run, problems=problems,
    )
