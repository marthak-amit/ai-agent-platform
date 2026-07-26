"""
In-process cache for plan configuration (app/models/plan.py).

Plan config is read on nearly every conversation/message and image
generation, so caching avoids a DB round-trip per check. This is a
single-instance, in-memory cache — same caveat as the rate limiter
documented in CLAUDE.md: correct on one Railway worker, but workers don't
share this dict. A 5-minute TTL bounds staleness across workers even if an
admin update's invalidate() call only reaches the worker that served the
request.

Redis upgrade path (mirrors the one documented for rate limiting): replace
the module-level dict with a Redis hash keyed by plan_id, and invalidate()
with a DEL + pub/sub so every worker picks up the change instantly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan

_CACHE_TTL = timedelta(minutes=5)

_cache: dict[str, dict] = {}
_loaded_at: Optional[datetime] = None


def _plan_to_dict(plan: Plan) -> dict:
    """Convert a Plan ORM row into a plain dict, safe to cache across sessions."""
    return {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "price_inr": plan.price_inr,
        "conv_limit": plan.conv_limit,
        "image_quota": plan.image_quota,
        "image_overage_price": plan.image_overage_price,
        "daily_msg_limit": plan.daily_msg_limit,
        "channels": list(plan.channels),
        "campaign_allowed": plan.campaign_allowed,
        "campaign_max_recipients": plan.campaign_max_recipients,
        "campaign_monthly_limit": plan.campaign_monthly_limit,
        "tier_order": plan.tier_order,
        "description": plan.description,
        "is_active": plan.is_active,
    }


def _is_stale() -> bool:
    """Return True if the cache has never been loaded or has passed its TTL."""
    return _loaded_at is None or (datetime.now(timezone.utc) - _loaded_at) > _CACHE_TTL


async def _reload(db: AsyncSession) -> None:
    """Reload every plan row from the DB into the in-process cache."""
    global _loaded_at
    result = await db.execute(select(Plan))
    _cache.clear()
    for plan in result.scalars().all():
        _cache[plan.plan_id] = _plan_to_dict(plan)
    _loaded_at = datetime.now(timezone.utc)


async def get_all_plans(db: AsyncSession) -> list[dict]:
    """
    Return all plan dicts ordered by tier_order.

    Reloads from the DB first if the cache is empty or past its TTL.

    Args:
        db: Active async DB session.

    Returns:
        List of plan config dicts, starter-to-pro order.
    """
    if _is_stale():
        await _reload(db)
    return sorted(_cache.values(), key=lambda p: p["tier_order"])


async def get_plan(db: AsyncSession, plan_id: str) -> Optional[dict]:
    """
    Return one plan's config dict by plan_id.

    Args:
        db:      Active async DB session.
        plan_id: Plan identifier, e.g. "starter".

    Returns:
        Plan config dict, or None if plan_id is unknown.
    """
    if _is_stale():
        await _reload(db)
    return _cache.get(plan_id)


def invalidate() -> None:
    """Clear the in-process cache. Call after any admin write to the plans table."""
    global _loaded_at
    _cache.clear()
    _loaded_at = None
