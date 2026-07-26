"""
Tests for app/services/plan_service.py and app/routers/plans.py.

Plan config now lives in the `plans` DB table (see app/models/plan.py) and
is read through plan_cache — tests use the `seeded_plans` fixture to
pre-load the cache with the standard starter/growth/pro rows, bypassing
the DB entirely for plan lookups.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.client import Client
from app.services import plan_service


# ── plan_service unit tests ───────────────────────────────────────────────────

async def test_list_plans_returns_three_plans(seeded_plans, mock_db):
    """list_plans returns all three plans in tier order."""
    plans = await plan_service.list_plans(mock_db)
    assert len(plans) == 3
    assert [p["plan_id"] for p in plans] == ["starter", "growth", "pro"]


async def test_plan_prices(seeded_plans, mock_db):
    """Each plan has the correct INR price."""
    plans = {p["plan_id"]: p for p in await plan_service.list_plans(mock_db)}
    assert plans["starter"]["price_inr"] == 1499
    assert plans["growth"]["price_inr"] == 3999
    assert plans["pro"]["price_inr"] == 9999


async def test_plan_conv_and_image_limits(seeded_plans, mock_db):
    """Each plan enforces the correct conv_limit, image_quota, and overage rate."""
    plans = {p["plan_id"]: p for p in await plan_service.list_plans(mock_db)}
    assert (plans["starter"]["conv_limit"], plans["starter"]["image_quota"], plans["starter"]["image_overage_price"]) == (700, 20, 8)
    assert (plans["growth"]["conv_limit"], plans["growth"]["image_quota"], plans["growth"]["image_overage_price"]) == (2000, 50, 6)
    assert (plans["pro"]["conv_limit"], plans["pro"]["image_quota"], plans["pro"]["image_overage_price"]) == (6000, 100, 5)


async def test_plan_daily_limits(seeded_plans, mock_db):
    """Each plan enforces the correct daily message limit."""
    plans = {p["plan_id"]: p for p in await plan_service.list_plans(mock_db)}
    assert plans["starter"]["daily_msg_limit"] == 100
    assert plans["growth"]["daily_msg_limit"] == 300
    assert plans["pro"]["daily_msg_limit"] == 700


async def test_plan_channels(seeded_plans, mock_db):
    """Starter has WhatsApp only; growth adds Instagram; pro adds website."""
    plans = {p["plan_id"]: p for p in await plan_service.list_plans(mock_db)}
    assert plans["starter"]["channels"] == ["whatsapp"]
    assert set(plans["growth"]["channels"]) == {"whatsapp", "instagram"}
    assert set(plans["pro"]["channels"]) == {"whatsapp", "instagram", "website"}


async def test_get_plan_returns_dict(seeded_plans, mock_db):
    """get_plan returns the correct plan dict for known plan ids."""
    assert (await plan_service.get_plan(mock_db, "starter"))["name"] == "Starter"
    assert (await plan_service.get_plan(mock_db, "growth"))["name"] == "Growth"
    assert (await plan_service.get_plan(mock_db, "pro"))["name"] == "Pro"


async def test_get_plan_unknown_returns_none(seeded_plans, mock_db):
    """get_plan returns None for unknown plan ids."""
    assert await plan_service.get_plan(mock_db, "enterprise") is None


# ── plan_allows_channel ───────────────────────────────────────────────────────

async def test_starter_allows_whatsapp_only(seeded_plans, mock_db):
    """Starter plan allows whatsapp but blocks instagram and website."""
    assert await plan_service.plan_allows_channel(mock_db, "starter", "whatsapp") is True
    assert await plan_service.plan_allows_channel(mock_db, "starter", "instagram") is False
    assert await plan_service.plan_allows_channel(mock_db, "starter", "website") is False


async def test_growth_allows_whatsapp_and_instagram(seeded_plans, mock_db):
    """Growth plan allows whatsapp and instagram but blocks website."""
    assert await plan_service.plan_allows_channel(mock_db, "growth", "whatsapp") is True
    assert await plan_service.plan_allows_channel(mock_db, "growth", "instagram") is True
    assert await plan_service.plan_allows_channel(mock_db, "growth", "website") is False


async def test_pro_allows_all_channels(seeded_plans, mock_db):
    """Pro plan allows all three channels."""
    assert await plan_service.plan_allows_channel(mock_db, "pro", "whatsapp") is True
    assert await plan_service.plan_allows_channel(mock_db, "pro", "instagram") is True
    assert await plan_service.plan_allows_channel(mock_db, "pro", "website") is True


async def test_unknown_plan_defaults_to_starter_permissions(seeded_plans, mock_db):
    """Unknown plan id falls back to starter permissions."""
    assert await plan_service.plan_allows_channel(mock_db, "unknown_plan", "instagram") is False
    assert await plan_service.plan_allows_channel(mock_db, "unknown_plan", "whatsapp") is True


# ── upgrade_plan ──────────────────────────────────────────────────────────────

async def test_upgrade_starter_to_growth(seeded_plans, mock_db):
    """Upgrading from starter to growth refreshes plan_slug and all snapshot fields."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)

    result = await plan_service.upgrade_plan(mock_db, client, "growth")

    assert client.plan_slug == "growth"
    assert client.daily_message_limit == 300
    assert client.plan_conv_limit_snapshot == 2000
    assert client.plan_price_snapshot == 3999
    assert client.plan_image_quota_snapshot == 50
    assert client.plan_image_overage_price_snapshot == 6
    assert client.plan_grandfathered is False
    assert result["plan_id"] == "growth"
    mock_db.commit.assert_called()


async def test_upgrade_starter_to_pro(seeded_plans, mock_db):
    """Upgrading directly from starter to pro is allowed."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)

    result = await plan_service.upgrade_plan(mock_db, client, "pro")

    assert client.plan_slug == "pro"
    assert client.daily_message_limit == 700
    assert client.plan_conv_limit_snapshot == 6000
    assert result["plan_id"] == "pro"


async def test_upgrade_same_plan_raises(seeded_plans, mock_db):
    """Upgrading to the current plan raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="growth", daily_message_limit=300)

    with pytest.raises(ValueError, match="not an upgrade"):
        await plan_service.upgrade_plan(mock_db, client, "growth")


async def test_downgrade_raises(seeded_plans, mock_db):
    """Attempting a downgrade raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="pro", daily_message_limit=700)

    with pytest.raises(ValueError, match="not an upgrade"):
        await plan_service.upgrade_plan(mock_db, client, "starter")


async def test_upgrade_unknown_plan_raises(seeded_plans, mock_db):
    """Upgrading to an unknown plan id raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)

    with pytest.raises(ValueError, match="Unknown plan"):
        await plan_service.upgrade_plan(mock_db, client, "enterprise")


# ── plans router tests ────────────────────────────────────────────────────────

def test_list_plans_endpoint_returns_200(client, seeded_plans):
    """GET /plans returns 200 with three plans (no auth required)."""
    response = client.get("/plans")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    slugs = [p["slug"] for p in data]
    assert slugs == ["starter", "growth", "pro"]


def test_list_plans_endpoint_includes_price_and_channels(client, seeded_plans):
    """GET /plans includes price_inr, conv_limit, image_quota, and channels for each plan."""
    response = client.get("/plans")
    assert response.status_code == 200
    starter = next(p for p in response.json() if p["slug"] == "starter")
    assert starter["price_inr"] == 1499
    assert starter["conv_limit"] == 700
    assert starter["image_quota"] == 20
    assert starter["image_overage_price"] == 8
    assert starter["channels"] == ["whatsapp"]


def test_get_current_plan_returns_starter(client, mock_db, mock_settings, make_test_user, seeded_plans):
    """GET /plans/current returns starter for a new client."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter",
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.get("/plans/current", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["slug"] == "starter"
    assert response.json()["price_inr"] == 1499


def test_get_current_plan_requires_auth(client, seeded_plans):
    """GET /plans/current returns 401 without a token."""
    response = client.get("/plans/current")
    assert response.status_code == 401


def test_upgrade_plan_returns_200(client, mock_db, mock_settings, make_test_user, seeded_plans):
    """POST /plans/upgrade from starter to growth returns 200 with upgrade details."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter", daily_message_limit=100,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=lambda obj: None)

    response = client.post(
        "/plans/upgrade",
        json={"plan_slug": "growth"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["previous_plan"] == "starter"
    assert data["new_plan"]["slug"] == "growth"
    assert "upgraded" in data["message"].lower()


def test_upgrade_plan_downgrade_returns_400(client, mock_db, mock_settings, make_test_user, seeded_plans):
    """POST /plans/upgrade with a downgrade returns 400."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="pro", daily_message_limit=700,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/plans/upgrade",
        json={"plan_slug": "starter"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


def test_upgrade_plan_unknown_slug_returns_400(client, mock_db, mock_settings, make_test_user, seeded_plans):
    """POST /plans/upgrade with an unknown plan slug returns 400."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter", daily_message_limit=100,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/plans/upgrade",
        json={"plan_slug": "ultra"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


def test_upgrade_plan_requires_auth(client, seeded_plans):
    """POST /plans/upgrade returns 401 without a token."""
    response = client.post("/plans/upgrade", json={"plan_slug": "growth"})
    assert response.status_code == 401


# ── Instagram plan guard tests ────────────────────────────────────────────────

def test_instagram_webhook_blocked_on_starter(client, mock_db, seeded_plans):
    """POST /instagram returns plan_restricted when client is on starter plan."""
    from unittest.mock import patch

    with patch(
        "app.routers.instagram._verify_instagram_signature",
        return_value=True,
    ), patch(
        "app.routers.instagram._get_active_client_plan",
        new=AsyncMock(return_value="starter"),
    ):
        response = client.post(
            "/instagram",
            content=b'{"object":"instagram"}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "plan_restricted"


def test_instagram_webhook_allowed_on_growth(client, mock_db, seeded_plans):
    """POST /instagram proceeds past the plan guard on growth plan."""
    from unittest.mock import patch

    with patch(
        "app.routers.instagram._verify_instagram_signature",
        return_value=True,
    ), patch(
        "app.routers.instagram._get_active_client_plan",
        new=AsyncMock(return_value="growth"),
    ), patch(
        "app.routers.instagram.InstagramWebhookPayload.model_validate_json",
        side_effect=Exception("parse_error"),
    ):
        response = client.post(
            "/instagram",
            content=b'{}',
            headers={"Content-Type": "application/json"},
        )

    # parse_error means it got past the plan guard
    assert response.status_code == 200
    assert response.json()["status"] == "parse_error"


def test_instagram_webhook_allowed_on_pro(client, mock_db, seeded_plans):
    """POST /instagram proceeds past the plan guard on pro plan."""
    from unittest.mock import patch

    with patch(
        "app.routers.instagram._verify_instagram_signature",
        return_value=True,
    ), patch(
        "app.routers.instagram._get_active_client_plan",
        new=AsyncMock(return_value="pro"),
    ), patch(
        "app.routers.instagram.InstagramWebhookPayload.model_validate_json",
        side_effect=Exception("parse_error"),
    ):
        response = client.post(
            "/instagram",
            content=b'{}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "parse_error"
