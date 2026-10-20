"""
Account lifecycle for the dashboard-first onboarding: quick start, sample products, setup checklist, go live.

There is no forced multi-step wizard any more. A new account lands on the dashboard (after one quick-start screen
asking for business name + language) with:

  * three sample products (`Product.is_demo`) so the simulator has something to sell, removable in one click;
  * a setup checklist whose status is DERIVED from real state — product count, agent config, channel credentials,
    a passed simulator test, `bot_active` — never ticked by hand;
  * account status 'setup': unlimited simulator, no trial, no expiry, and the bot does not answer real customers.

"Go live" is the only transition out of setup: it flips `bot_active`, starts the free trial (or just goes live on a
running plan) and claims the trial of the WhatsApp phone_number_id. See trial_service for the trial rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.trial_claim import TrialClaim
from app.services import catalogue_service, realtime_service, trial_service

logger = logging.getLogger(__name__)

#: Checklist rows in display order, with whether they block "Go live".
CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("add_products", True),
    ("setup_agent", True),
    ("connect_whatsapp", True),
    ("connect_instagram", False),
    ("test_agent", True),
    ("go_live", True),
)
#: Items that must be done before "Go live" is allowed.
GO_LIVE_PREREQUISITES = ("add_products", "setup_agent", "connect_whatsapp", "test_agent")


class LifecycleError(Exception):
    """A refused lifecycle action; `code` is the machine-readable reason surfaced to the dashboard."""

    def __init__(self, code: str, message: str, **extra: object) -> None:
        """Store the error code, the human message and any extra context (e.g. the missing checklist keys)."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# ── quick start ────────────────────────────────────────────────────────────────

def _slug_base(name: str) -> str:
    """URL-safe catalogue slug stem from a business name; falls back to 'shop' for names with no ASCII letters."""
    slug = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    slug = re.sub(r"\s+", "-", slug.strip())[:50].strip("-")
    return slug or "shop"


async def ensure_catalogue_slug(db: AsyncSession, client: Client) -> None:
    """Give *client* a unique public-catalogue slug derived from its business name if it has none."""
    if client.catalogue_slug:
        return
    base = _slug_base(client.business_name or "")
    slug, n = base, 1
    while True:
        taken = await db.execute(select(Client.id).where(Client.catalogue_slug == slug, Client.id != client.id).limit(1))
        if taken.scalar_one_or_none() is None:
            break
        slug = f"{base}-{n}"
        n += 1
    client.catalogue_slug = slug


#: Sample catalogue seeded into a new account: (name, category, price, description, variants-or-stock).
_DEMO_PRODUCTS: tuple[dict, ...] = (
    {
        "name": "Cotton Kurti", "category": "Kurti", "price": 799.0,
        "description": "Soft everyday cotton kurti with a straight fit.",
        "variants": [
            {"color": "Pink", "size": "M", "stock": 10}, {"color": "Pink", "size": "L", "stock": 10},
            {"color": "Blue", "size": "M", "stock": 10}, {"color": "Blue", "size": "L", "stock": 10},
        ],
    },
    {
        "name": "Banarasi Silk Saree", "category": "Saree", "price": 2499.0,
        "description": "Woven Banarasi silk saree with a rich zari border.",
        "variants": [{"color": "Red", "size": None, "stock": 5}, {"color": "Green", "size": None, "stock": 5}],
    },
    {
        "name": "Handmade Tote Bag", "category": "Bag", "price": 349.0,
        "description": "Sturdy handmade cotton tote bag.", "stock": 25,
    },
)


async def seed_demo_products(db: AsyncSession, client: Client) -> int:
    """
    Create the three sample products for a brand-new account. Idempotent: does nothing when the catalogue is not empty.

    Returns the number of products created.
    """
    existing = await db.scalar(select(func.count()).select_from(Product).where(Product.client_id == client.id))
    if existing:
        return 0
    created = 0
    for spec in _DEMO_PRODUCTS:
        variants = spec.get("variants")
        product = await catalogue_service.create_product(
            db, client.id, spec["name"], spec["price"],
            stock=sum(v["stock"] for v in variants) if variants else spec.get("stock"),
            description=spec["description"], category=spec["category"],
            has_variants=bool(variants), is_demo=True,
        )
        if variants:
            await catalogue_service.create_product_variants(db, product.id, client.id, variants)
        created += 1
    return created


async def count_demo_products(db: AsyncSession, client_id: int) -> int:
    """Sample products still in the catalogue."""
    return int(await db.scalar(
        select(func.count()).select_from(Product).where(Product.client_id == client_id, Product.is_demo.is_(True))
    ) or 0)


async def remove_demo_products(db: AsyncSession, client: Client) -> int:
    """
    Delete every sample product (and its variants) of *client*. Real products are never touched.

    Past simulator orders keep their name/SKU snapshot (their product link is cleared by delete_product).
    Returns the number of products removed.
    """
    products = (
        await db.execute(select(Product).where(Product.client_id == client.id, Product.is_demo.is_(True)))
    ).scalars().all()
    removed = 0
    for product in products:
        for variant in (await db.execute(select(ProductVariant).where(ProductVariant.product_id == product.id))).scalars().all():
            await db.delete(variant)
        await db.flush()
        await db.refresh(product)
        await catalogue_service.delete_product(db, product)
        removed += 1
    return removed


async def quick_start(db: AsyncSession, client: Client, business_name: str, language: str) -> int:
    """
    The single screen after the first login: save business name + dashboard language, seed the samples.

    Re-submitting only updates the name/language — samples are seeded once, the first time. Returns the number of
    sample products created by THIS call.
    """
    await db.refresh(client, with_for_update=True)      # serialise a double-submitted form: seed exactly once
    first_time = client.quick_start_completed_at is None
    client.business_name = business_name
    client.dashboard_language = language
    await ensure_catalogue_slug(db, client)
    created = 0
    if first_time:
        client.quick_start_completed_at = datetime.now(timezone.utc)
        await db.commit()
        created = await seed_demo_products(db, client)
    else:
        await db.commit()
    await db.refresh(client)
    return created


# ── checklist ──────────────────────────────────────────────────────────────────

async def _real_product_count(db: AsyncSession, client_id: int) -> int:
    """Active, non-sample products — what 'Add products' means."""
    return int(await db.scalar(
        select(func.count()).select_from(Product).where(
            Product.client_id == client_id, Product.is_demo.is_(False), Product.is_active.is_(True)
        )
    ) or 0)


async def checklist_state(db: AsyncSession, client: Client) -> dict[str, bool]:
    """Map every checklist key to done/not-done, from real database state."""
    return {
        "add_products": await _real_product_count(db, client.id) > 0,
        "setup_agent": bool(client.business_type and client.business_description),
        "connect_whatsapp": bool(client.whatsapp_phone_number_id and client.whatsapp_access_token),
        "connect_instagram": bool(client.instagram_access_token),
        "test_agent": client.agent_tested_at is not None,
        "go_live": bool(client.bot_active and client.go_live_at is not None) or _is_legacy_live(client),
    }


def _is_legacy_live(client: Client) -> bool:
    """An account that pre-dates the lifecycle: live (bot_active, status 'active') with no go-live timestamp."""
    return bool(client.bot_active and client.account_status == "active")


async def go_live_mode(db: AsyncSession, client: Client, now: datetime | None = None) -> str:
    """
    What pressing 'Go live' would do: 'live' (already), 'plan' (a plan is running / exempt account),
    'trial_used' (this WhatsApp number already had its trial and there is no plan) or 'trial'.
    """
    from app.services.billing import entitlement

    now = now or datetime.now(timezone.utc)
    if client.bot_active and (client.go_live_at is not None or _is_legacy_live(client)):
        return "live"
    ent = await entitlement.get_entitlement(db, client, now)
    if ent.state in (entitlement.EntitlementState.ACTIVE, entitlement.EntitlementState.EXEMPT):
        return "plan"
    if client.trial_started_at is not None:
        return "trial_used"      # this account already had its trial (and let it lapse)
    pid = client.whatsapp_phone_number_id
    if pid:
        owner = await db.scalar(select(TrialClaim.client_id).where(TrialClaim.phone_number_id == pid))
        if owner is not None and owner != client.id:
            return "trial_used"
    return "trial"


async def trial_summary(db: AsyncSession, client: Client, now: datetime | None = None) -> dict | None:
    """Trial standing as a plain dict for TrialOut, or None when the account never started a trial."""
    if client.trial_started_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    standing = await trial_service.trial_standing(db, client, now)
    return {
        "active": standing.active and client.account_status == "trial",
        "started_at": client.trial_started_at,
        "ends_at": client.trial_ends_at,
        "conversations_used": standing.used,
        "conversation_limit": standing.limit,
    }


async def build_checklist(db: AsyncSession, client: Client) -> dict:
    """Assemble the GET /onboarding/checklist payload."""
    state = await checklist_state(db, client)
    items = [{"key": key, "required": required, "done": state[key]} for key, required in CHECKLIST]
    required_done = all(state[k] for k, required in CHECKLIST if required)
    next_key = next((k for k, required in CHECKLIST if required and not state[k]), None)
    live = state["go_live"]
    return {
        "items": items,
        "done_count": sum(1 for i in items if i["done"]),
        "total": len(items),
        "all_required_done": required_done,
        "hidden": live or required_done,
        "next_key": next_key,
        "account_status": client.account_status,
        "bot_active": bool(client.bot_active),
        "quick_start_completed": client.quick_start_completed_at is not None,
        "demo_products": await count_demo_products(db, client.id),
        "can_go_live": all(state[k] for k in GO_LIVE_PREREQUISITES),
        "go_live_mode": await go_live_mode(db, client),
        "trial": await trial_summary(db, client),
        "trial_days": trial_service.trial_days(),
        "trial_conversation_limit": trial_service.trial_conversation_limit(),
    }


# ── go live ─────────────────────────────────────────────────────────────────────

@dataclass
class GoLiveResult:
    """What a successful go-live did."""

    demo_removed: int = 0
    trial_started: bool = False
    missing: list[str] = field(default_factory=list)


async def go_live(db: AsyncSession, client_id: int, *, remove_demo: bool = False) -> GoLiveResult:
    """
    Switch the bot on for real customers and start the trial (or go live on the running plan).

    Refused (LifecycleError) with:
      SETUP_INCOMPLETE      a prerequisite checklist item is not done (`missing` lists them)
      DEMO_PRODUCTS_PRESENT sample products remain and `remove_demo` was not set — customers must never be sold samples
      TRIAL_ALREADY_USED    no plan is running and this WhatsApp number (or account) already had its one trial
    Idempotent: an account that is already live returns without changes.
    """
    client = await db.get(Client, client_id, with_for_update=True)
    if client is None:
        raise LifecycleError("NOT_FOUND", "Account not found.")
    now = datetime.now(timezone.utc)
    mode = await go_live_mode(db, client, now)
    if mode == "live":
        return GoLiveResult()

    state = await checklist_state(db, client)
    missing = [k for k in GO_LIVE_PREREQUISITES if not state[k]]
    if missing:
        raise LifecycleError("SETUP_INCOMPLETE", "Finish the setup checklist before going live.", missing=missing)
    demo_count = await count_demo_products(db, client_id)
    if demo_count and not remove_demo:
        raise LifecycleError(
            "DEMO_PRODUCTS_PRESENT", "Remove the sample products before going live.", demo_products=demo_count,
        )
    if mode == "trial_used":
        raise LifecycleError(
            "TRIAL_ALREADY_USED",
            "This WhatsApp number has already used its free trial. Choose a plan to go live.",
        )

    result = GoLiveResult()
    if demo_count:
        result.demo_removed = await remove_demo_products(db, client)
        client = await db.get(Client, client_id, with_for_update=True)   # delete_product committed: re-lock the row

    if mode == "trial":
        if not await trial_service.claim_phone_number(db, client_id, client.whatsapp_phone_number_id):
            await db.rollback()     # lost a race against another account claiming the same number
            raise LifecycleError(
                "TRIAL_ALREADY_USED",
                "This WhatsApp number has already used its free trial. Choose a plan to go live.",
            )
        trial_service.start_trial(client, now)
        result.trial_started = True
    else:
        client.account_status = "active"      # a plan is running (or house account): no trial involved
    client.bot_active = True
    client.go_live_at = now
    await db.commit()
    await db.refresh(client)
    realtime_service.publish(client.id, "account_live", {"trial": result.trial_started})
    logger.info("go_live: client=%s mode=%s demo_removed=%s", client.id, mode, result.demo_removed)
    return result
