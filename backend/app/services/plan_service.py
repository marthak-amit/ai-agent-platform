"""
Plan service — plan lookups, channel guards, and upgrade logic.

Plan definitions live in the `plans` DB table (see app/models/plan.py) and
are read through plan_cache so admin price/limit changes take effect without
a redeploy. Never hardcode a plan's price, limits, or channels here — read
them from the table.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.services import plan_cache

# ── Plan lookups ──────────────────────────────────────────────────────────────


async def list_plans(db: AsyncSession) -> list[dict[str, Any]]:
    """
    Return all active plans in tier order (starter → growth → pro).

    Args:
        db: Active async DB session.

    Returns:
        List of plan config dicts.
    """
    plans = await plan_cache.get_all_plans(db)
    return [p for p in plans if p["is_active"]]


async def get_plan(db: AsyncSession, plan_id: str) -> Optional[dict[str, Any]]:
    """
    Return the plan definition for the given plan_id, or None if unknown.

    Args:
        db:      Active async DB session.
        plan_id: Plan identifier string.

    Returns:
        Plan config dict or None.
    """
    return await plan_cache.get_plan(db, plan_id)


async def plan_allows_channel(db: AsyncSession, plan_id: str, channel: str) -> bool:
    """
    Return True if the plan grants access to the given channel.

    Channel values: "whatsapp", "instagram", "website".
    Unknown plan ids default to starter permissions.

    Args:
        db:      Active async DB session.
        plan_id: Client's current plan id.
        channel: Channel name to check.

    Returns:
        True if the channel is included in the plan, False otherwise.
    """
    plan = await plan_cache.get_plan(db, plan_id)
    if plan is None:
        plan = await plan_cache.get_plan(db, "starter")
    return channel in plan["channels"]


# ── Upgrade logic ─────────────────────────────────────────────────────────────


async def upgrade_plan(
    db: AsyncSession, client: Client, new_plan_id: str
) -> dict[str, Any]:
    """
    Upgrade (or change) the client's plan to new_plan_id.

    Validates that:
      - new_plan_id is a recognised, active plan.
      - new_plan_id represents a higher tier than the client's current plan
        (downgrades must be handled via a separate billing flow).

    Refreshes daily_message_limit and all four billing snapshot columns to
    the new plan's live terms, and starts a fresh billing cycle immediately
    (upgrades are not grandfathered — they opt into the new plan's terms).

    Args:
        db:          Active async DB session.
        client:      The authenticated Client ORM instance.
        new_plan_id: Target plan id.

    Returns:
        The new plan's config dict.

    Raises:
        ValueError: If new_plan_id is unknown or is not an upgrade from the
            client's current plan.
    """
    new_plan = await plan_cache.get_plan(db, new_plan_id)
    all_plans = await plan_cache.get_all_plans(db)
    plan_ids = [p["plan_id"] for p in sorted(all_plans, key=lambda p: p["tier_order"])]

    if new_plan is None or not new_plan["is_active"]:
        raise ValueError(f"Unknown plan: '{new_plan_id}'. Valid options: {', '.join(plan_ids)}.")

    current_plan = await plan_cache.get_plan(db, client.plan_slug) or await plan_cache.get_plan(
        db, "starter"
    )
    if new_plan["tier_order"] <= current_plan["tier_order"]:
        raise ValueError(
            f"'{new_plan_id}' is not an upgrade from your current plan ('{client.plan_slug}'). "
            "To downgrade, please contact support."
        )

    client.plan_slug = new_plan_id
    client.daily_message_limit = new_plan["daily_msg_limit"]
    client.plan_conv_limit_snapshot = new_plan["conv_limit"]
    client.plan_price_snapshot = new_plan["price_inr"]
    client.plan_image_quota_snapshot = new_plan["image_quota"]
    client.plan_image_overage_price_snapshot = new_plan["image_overage_price"]
    client.billing_cycle_start = date.today()
    client.plan_grandfathered = False
    client.conv_limit_warned_period = None

    await db.commit()
    await db.refresh(client)
    return new_plan
