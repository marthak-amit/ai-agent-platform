"""Read models for the billing API: plan list with tenant-specific amounts, and subscription state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.client import Client
from app.models.sellertalk24_billing import BillingPlan, Invoice, PaymentOrder
from app.schemas.sellertalk24_billing import (
    AmountsOut,
    EntitlementOut,
    PaymentListOut,
    PaymentOut,
    PlanListOut,
    PlanOut,
    QueuedPeriodOut,
    SubscriptionOut,
    SubscriptionPlanOut,
    SubscriptionStateOut,
    UpgradeOptionOut,
)
from app.services.billing.entitlement import enforcement_enabled, get_entitlement
from app.services.billing.pricing import AmountBreakdown, compute_amounts
from app.services.billing.subscriptions import (
    days_left,
    get_active_subscription,
    get_plan_by_id,
    is_intra_state,
    list_active_plans,
    list_pending_subscriptions,
    now_utc,
    percent_used,
    plan_features,
    roll_forward,
    upgrade_credit_paise,
)

MAX_PAGE_SIZE = 100


def amounts_out(b: AmountBreakdown) -> AmountsOut:
    """Convert a pricing AmountBreakdown to its API schema."""
    return AmountsOut(
        base_paise=b.base_paise, credit_paise=b.credit_paise, taxable_paise=b.taxable_paise,
        cgst_paise=b.cgst_paise, sgst_paise=b.sgst_paise, igst_paise=b.igst_paise,
        gst_paise=b.gst_paise, total_paise=b.total_paise,
    )


async def build_plan_list(db: AsyncSession, client: Client, settings: Settings) -> PlanListOut:
    """Active plans in order, each with base/GST/total for this tenant's tax treatment."""
    intra = is_intra_state(client, settings.seller_state_code)
    active = await get_active_subscription(db, client.id)
    plans = []
    for plan in await list_active_plans(db):
        b = compute_amounts(plan, 0, intra_state=intra)
        plans.append(PlanOut(
            **amounts_out(b).model_dump(),
            code=plan.code, name=plan.name, conversation_limit=plan.conversation_limit,
            billing_period_days=plan.billing_period_days, features=plan_features(plan),
            sort_order=plan.sort_order, is_current=bool(active and active.plan_id == plan.id),
            prices_include_gst=settings.prices_include_gst, gst_rate_bps=settings.gst_rate_bps,
        ))
    return PlanListOut(plans=plans)


async def build_subscription_state(
    db: AsyncSession, client: Client, settings: Settings, now: datetime | None = None
) -> SubscriptionStateOut:
    """
    The tenant's current subscription, queued renewals, and upgrade options with credit preview.

    Applies the lazy roll-forward first (committing it), so an expired period is never shown as active.
    """
    now = now or now_utc()
    active = await roll_forward(db, client.id, now)
    await db.commit()
    ent = await get_entitlement(db, client, now)
    enforced = enforcement_enabled()
    entitlement = EntitlementOut(
        state=ent.state.value, grace_ends_at=ent.grace_ends_at, enforced=enforced,
        restricted=enforced and ent.restricted, over_limit=ent.over_limit,
    )
    if active is None:
        return SubscriptionStateOut(status="none", entitlement=entitlement, seller_state_code=settings.seller_state_code)

    plan = await get_plan_by_id(db, active.plan_id)
    pending = await list_pending_subscriptions(db, client.id)
    intra = is_intra_state(client, settings.seller_state_code)

    queued = []
    for p in pending:
        queued_plan = await get_plan_by_id(db, p.plan_id)
        queued.append(QueuedPeriodOut(
            id=p.id, plan_code=queued_plan.code, plan_name=queued_plan.name,
            current_period_start=p.current_period_start, current_period_end=p.current_period_end,
        ))

    credit = await upgrade_credit_paise(db, active, pending, now)
    options = []
    for candidate in await list_active_plans(db):
        if candidate.sort_order <= plan.sort_order:
            continue
        options.append(UpgradeOptionOut(
            plan_code=candidate.code, plan_name=candidate.name,
            conversation_limit=candidate.conversation_limit,
            amounts=amounts_out(compute_amounts(candidate, credit, intra_state=intra)),
        ))

    return SubscriptionStateOut(
        status="active",
        entitlement=entitlement,
        over_limit=bool(active.over_limit),
        seller_state_code=settings.seller_state_code,
        subscription=SubscriptionOut(
            id=active.id, status=active.status,
            plan=SubscriptionPlanOut(code=plan.code, name=plan.name, features=plan_features(plan)),
            current_period_start=active.current_period_start, current_period_end=active.current_period_end,
            conversations_used=active.conversations_used, conversation_limit=active.conversation_limit,
            percent_used=percent_used(active), days_left=days_left(active, now),
        ),
        queued=queued,
        upgrade_options=options,
    )


async def build_payment_list(db: AsyncSession, client_id: int, page: int, page_size: int) -> PaymentListOut:
    """One page of the tenant's payment orders, newest first."""
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    total = await db.scalar(select(func.count()).select_from(PaymentOrder).where(PaymentOrder.client_id == client_id))
    rows = (
        await db.execute(
            select(PaymentOrder, BillingPlan.code, BillingPlan.name, Invoice.id, Invoice.invoice_number)
            .join(BillingPlan, BillingPlan.id == PaymentOrder.plan_id)
            .outerjoin(Invoice, Invoice.payment_order_id == PaymentOrder.id)
            .where(PaymentOrder.client_id == client_id)
            .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = [
        PaymentOut(
            id=o.id, plan_code=code, plan_name=name, purpose=o.purpose, status=o.status,
            razorpay_order_id=o.razorpay_order_id, razorpay_payment_id=o.razorpay_payment_id,
            currency=o.currency, amount_paise=o.amount_paise, base_paise=o.base_paise,
            credit_paise=o.credit_paise, taxable_paise=o.taxable_paise, cgst_paise=o.cgst_paise,
            sgst_paise=o.sgst_paise, igst_paise=o.igst_paise, gst_paise=o.gst_paise,
            failure_reason=o.failure_reason, created_at=o.created_at, paid_at=o.paid_at,
            invoice_id=invoice_id, invoice_number=invoice_number,
        )
        for o, code, name, invoice_id, invoice_number in rows
    ]
    return PaymentListOut(items=items, total=int(total or 0), page=page, page_size=page_size)
