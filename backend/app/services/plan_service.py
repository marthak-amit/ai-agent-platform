"""
Plan service — plan definitions, channel guards, and upgrade logic.

Plans are static business configuration (never mutated at runtime), so they
live here as Python constants rather than in the database. The client's chosen
plan is stored as a slug string on the Client model.

Plan hierarchy (tier order for upgrade validation):
    starter (1) < growth (2) < pro (3)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client

# ── Plan definitions ──────────────────────────────────────────────────────────

PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "slug": "starter",
        "name": "Starter",
        "price_inr": 999,
        "daily_msg_limit": 100,
        "channels": ["whatsapp"],
        "description": "Perfect for small businesses getting started with WhatsApp automation.",
    },
    "growth": {
        "slug": "growth",
        "name": "Growth",
        "price_inr": 1999,
        "daily_msg_limit": 300,
        "channels": ["whatsapp", "instagram"],
        "description": "Scale your reach across WhatsApp and Instagram.",
    },
    "pro": {
        "slug": "pro",
        "name": "Pro",
        "price_inr": 3999,
        "daily_msg_limit": 700,
        "channels": ["whatsapp", "instagram", "website"],
        "description": "Full omnichannel presence — WhatsApp, Instagram, and website widget.",
    },
}

# Ordered slugs for tier comparison (index = tier level).
_PLAN_ORDER = ["starter", "growth", "pro"]


def list_plans() -> list[dict[str, Any]]:
    """
    Return all plans in tier order (starter → growth → pro).

    Returns:
        List of plan dicts, each containing slug, name, price_inr,
        daily_msg_limit, channels, and description.
    """
    return [PLANS[slug] for slug in _PLAN_ORDER]


def get_plan(slug: str) -> dict[str, Any] | None:
    """
    Return the plan definition for the given slug, or None if unknown.

    Args:
        slug: Plan identifier string.

    Returns:
        Plan dict or None.
    """
    return PLANS.get(slug)


def plan_allows_channel(plan_slug: str, channel: str) -> bool:
    """
    Return True if the plan grants access to the given channel.

    Channel values: "whatsapp", "instagram", "website".
    Unknown plan slugs default to starter permissions.

    Args:
        plan_slug: Client's current plan slug.
        channel:   Channel name to check.

    Returns:
        True if the channel is included in the plan, False otherwise.
    """
    plan = PLANS.get(plan_slug, PLANS["starter"])
    return channel in plan["channels"]


def _tier(slug: str) -> int:
    """Return the integer tier index for a plan slug (0-based)."""
    try:
        return _PLAN_ORDER.index(slug)
    except ValueError:
        return 0


# ── Upgrade logic ─────────────────────────────────────────────────────────────

async def upgrade_plan(
    db: AsyncSession, client: Client, new_slug: str
) -> dict[str, Any]:
    """
    Upgrade (or change) the client's plan to new_slug.

    Validates that:
      - new_slug is a recognised plan.
      - new_slug represents a higher tier than the client's current plan
        (downgrades must be handled via a separate billing flow).

    Also updates daily_message_limit to match the new plan so that the
    usage service enforces the correct quota immediately.

    Args:
        db:       Active async DB session.
        client:   The authenticated Client ORM instance.
        new_slug: Target plan slug.

    Returns:
        The new plan's definition dict.

    Raises:
        ValueError: If new_slug is unknown or is not an upgrade from current plan.
    """
    if new_slug not in PLANS:
        raise ValueError(f"Unknown plan: '{new_slug}'. Valid options: {', '.join(_PLAN_ORDER)}.")

    if _tier(new_slug) <= _tier(client.plan_slug):
        raise ValueError(
            f"'{new_slug}' is not an upgrade from your current plan ('{client.plan_slug}'). "
            "To downgrade, please contact support."
        )

    new_plan = PLANS[new_slug]
    client.plan_slug = new_slug
    client.daily_message_limit = new_plan["daily_msg_limit"]

    await db.commit()
    await db.refresh(client)
    return new_plan
