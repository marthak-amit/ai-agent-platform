"""
Admin service — platform-wide queries for the admin control panel.

All functions operate across all clients (no ownership scoping).
Revenue figures are computed from active-client plan subscriptions;
the platform does not yet store historical billing records.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.plan import Plan
from app.models.usage_log import UsageLog
from app.services import plan_cache


# ── Client listing ─────────────────────────────────────────────────────────────

async def get_all_clients(db: AsyncSession) -> list[dict]:
    """
    Return all clients with their today/monthly usage and plan revenue.

    Executes three queries total (clients, today's usage map, monthly usage map)
    and joins them in Python to avoid N+1 queries.

    Args:
        db: Active async DB session.

    Returns:
        List of dicts with id, email, business_name, plan_slug, is_active,
        messages_today, messages_this_month, monthly_revenue_inr, created_at.
    """
    today = date.today()
    first_of_month = today.replace(day=1)

    clients_result = await db.execute(select(Client).order_by(Client.id))
    clients = list(clients_result.scalars().all())

    today_result = await db.execute(
        select(UsageLog.client_id, UsageLog.message_count).where(UsageLog.date == today)
    )
    today_map: dict[int, int] = {row.client_id: row.message_count for row in today_result}

    monthly_result = await db.execute(
        select(
            UsageLog.client_id,
            func.sum(UsageLog.message_count).label("total"),
        )
        .where(UsageLog.date >= first_of_month)
        .group_by(UsageLog.client_id)
    )
    monthly_map: dict[int, int] = {row.client_id: int(row.total) for row in monthly_result}

    return [
        {
            "id": c.id,
            "email": c.email,
            "business_name": c.business_name,
            "plan_slug": c.plan_slug or "starter",
            "is_active": c.is_active,
            "messages_today": today_map.get(c.id, 0),
            "messages_this_month": monthly_map.get(c.id, 0),
            "monthly_revenue_inr": c.plan_price_snapshot,
            "created_at": c.created_at,
        }
        for c in clients
    ]


# ── Platform stats ────────────────────────────────────────────────────────────

async def get_platform_stats(db: AsyncSession) -> dict:
    """
    Return aggregate platform statistics.

    Queries:
      1. Count of active clients.
      2. Sum of active clients' plan_price_snapshot → actual invoiced revenue.
      3. Sum of today's messages across all clients.
      4. Sum of this month's messages across all clients.

    Args:
        db: Active async DB session.

    Returns:
        Dict with active_clients, monthly_revenue_inr, messages_today,
        messages_this_month.
    """
    today = date.today()
    first_of_month = today.replace(day=1)

    active_result = await db.execute(
        select(func.count(Client.id)).where(Client.is_active == True)  # noqa: E712
    )
    active_clients = int(active_result.scalar() or 0)

    revenue_result = await db.execute(
        select(func.sum(Client.plan_price_snapshot)).where(Client.is_active == True)  # noqa: E712
    )
    monthly_revenue_inr = int(revenue_result.scalar() or 0)

    today_msgs_result = await db.execute(
        select(func.sum(UsageLog.message_count)).where(UsageLog.date == today)
    )
    messages_today = int(today_msgs_result.scalar() or 0)

    monthly_msgs_result = await db.execute(
        select(func.sum(UsageLog.message_count)).where(UsageLog.date >= first_of_month)
    )
    messages_this_month = int(monthly_msgs_result.scalar() or 0)

    return {
        "active_clients": active_clients,
        "monthly_revenue_inr": monthly_revenue_inr,
        "messages_today": messages_today,
        "messages_this_month": messages_this_month,
    }


# ── Suspend / activate ────────────────────────────────────────────────────────

async def set_client_active(db: AsyncSession, client_id: int, active: bool) -> Client:
    """
    Set a client's is_active flag.

    Args:
        db:        Active async DB session.
        client_id: Target client primary key.
        active:    True to activate, False to suspend.

    Returns:
        Updated Client instance.

    Raises:
        ValueError: If client_id does not exist.
    """
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if client is None:
        raise ValueError(f"Client {client_id} not found.")
    client.is_active = active
    await db.commit()
    await db.refresh(client)
    return client


# ── Revenue breakdown ─────────────────────────────────────────────────────────

async def get_revenue_breakdown(db: AsyncSession) -> dict:
    """
    Return monthly revenue broken down by plan, counting only active clients.

    Revenue per client uses plan_price_snapshot (the price they actually
    agreed to at their last cycle rollover/upgrade) rather than the live
    plan price, so past pricing changes don't distort historical breakdowns.

    Args:
        db: Active async DB session.

    Returns:
        Dict with month (YYYY-MM), total_revenue_inr, and a breakdown list
        of {plan, plan_name, client_count, revenue_inr} per tier.
    """
    today = date.today()
    plans = await plan_cache.get_all_plans(db)

    dist_result = await db.execute(
        select(
            Client.plan_slug,
            func.count(Client.id).label("cnt"),
            func.sum(Client.plan_price_snapshot).label("revenue"),
        )
        .where(Client.is_active == True)  # noqa: E712
        .group_by(Client.plan_slug)
    )
    dist_map: dict[str, tuple[int, int]] = {
        (slug or "starter"): (int(cnt), int(revenue or 0)) for slug, cnt, revenue in dist_result
    }

    breakdown = []
    total = 0
    for plan in plans:
        slug = plan["plan_id"]
        count, revenue = dist_map.get(slug, (0, 0))
        breakdown.append(
            {
                "plan": slug,
                "plan_name": plan["name"],
                "client_count": count,
                "revenue_inr": revenue,
            }
        )
        total += revenue

    return {
        "month": today.strftime("%Y-%m"),
        "total_revenue_inr": total,
        "breakdown": breakdown,
    }


# ── Plan admin management ─────────────────────────────────────────────────────

async def update_plan(db: AsyncSession, plan_id: str, updates: dict[str, Any]) -> Plan:
    """
    Apply a partial update to a plan row and invalidate the plan cache.

    Only the keys present in `updates` (already validated non-negative by
    PlanUpdateRequest) are changed. Does not touch any client's snapshot
    columns — those move only via billing_service.ensure_current_cycle on
    each client's own next billing cycle, or immediately on upgrade.

    Args:
        db:      Active async DB session.
        plan_id: Target plan's primary key.
        updates: Dict of field -> new value (only non-None fields from the
                 request body).

    Returns:
        The updated Plan ORM instance.

    Raises:
        ValueError: If plan_id does not exist.
    """
    result = await db.execute(select(Plan).where(Plan.plan_id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise ValueError(f"Plan '{plan_id}' not found.")

    for field, value in updates.items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    plan_cache.invalidate()
    return plan
