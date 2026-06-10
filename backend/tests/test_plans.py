"""
Tests for app/services/plan_service.py and app/routers/plans.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.client import Client
from app.services import plan_service


# ── plan_service unit tests ───────────────────────────────────────────────────

def test_list_plans_returns_three_plans():
    """list_plans returns all three plans in tier order."""
    plans = plan_service.list_plans()
    assert len(plans) == 3
    assert [p["slug"] for p in plans] == ["starter", "growth", "pro"]


def test_plan_prices():
    """Each plan has the correct INR price."""
    plans = {p["slug"]: p for p in plan_service.list_plans()}
    assert plans["starter"]["price_inr"] == 999
    assert plans["growth"]["price_inr"] == 1999
    assert plans["pro"]["price_inr"] == 3999


def test_plan_daily_limits():
    """Each plan enforces the correct daily message limit."""
    plans = {p["slug"]: p for p in plan_service.list_plans()}
    assert plans["starter"]["daily_msg_limit"] == 100
    assert plans["growth"]["daily_msg_limit"] == 300
    assert plans["pro"]["daily_msg_limit"] == 700


def test_plan_channels():
    """Starter has WhatsApp only; growth adds Instagram; pro adds website."""
    plans = {p["slug"]: p for p in plan_service.list_plans()}
    assert plans["starter"]["channels"] == ["whatsapp"]
    assert set(plans["growth"]["channels"]) == {"whatsapp", "instagram"}
    assert set(plans["pro"]["channels"]) == {"whatsapp", "instagram", "website"}


def test_get_plan_returns_dict():
    """get_plan returns the correct plan dict for known slugs."""
    assert plan_service.get_plan("starter")["name"] == "Starter"
    assert plan_service.get_plan("growth")["name"] == "Growth"
    assert plan_service.get_plan("pro")["name"] == "Pro"


def test_get_plan_unknown_returns_none():
    """get_plan returns None for unknown slugs."""
    assert plan_service.get_plan("enterprise") is None


# ── plan_allows_channel ───────────────────────────────────────────────────────

def test_starter_allows_whatsapp_only():
    """Starter plan allows whatsapp but blocks instagram and website."""
    assert plan_service.plan_allows_channel("starter", "whatsapp") is True
    assert plan_service.plan_allows_channel("starter", "instagram") is False
    assert plan_service.plan_allows_channel("starter", "website") is False


def test_growth_allows_whatsapp_and_instagram():
    """Growth plan allows whatsapp and instagram but blocks website."""
    assert plan_service.plan_allows_channel("growth", "whatsapp") is True
    assert plan_service.plan_allows_channel("growth", "instagram") is True
    assert plan_service.plan_allows_channel("growth", "website") is False


def test_pro_allows_all_channels():
    """Pro plan allows all three channels."""
    assert plan_service.plan_allows_channel("pro", "whatsapp") is True
    assert plan_service.plan_allows_channel("pro", "instagram") is True
    assert plan_service.plan_allows_channel("pro", "website") is True


def test_unknown_plan_defaults_to_starter_permissions():
    """Unknown plan slug falls back to starter permissions."""
    assert plan_service.plan_allows_channel("unknown_plan", "instagram") is False
    assert plan_service.plan_allows_channel("unknown_plan", "whatsapp") is True


# ── upgrade_plan ──────────────────────────────────────────────────────────────

async def test_upgrade_starter_to_growth():
    """Upgrading from starter to growth updates plan_slug and daily_message_limit."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    result = await plan_service.upgrade_plan(db, client, "growth")

    assert client.plan_slug == "growth"
    assert client.daily_message_limit == 300
    assert result["slug"] == "growth"
    db.commit.assert_called_once()


async def test_upgrade_starter_to_pro():
    """Upgrading directly from starter to pro is allowed."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    result = await plan_service.upgrade_plan(db, client, "pro")

    assert client.plan_slug == "pro"
    assert client.daily_message_limit == 700
    assert result["slug"] == "pro"


async def test_upgrade_same_plan_raises():
    """Upgrading to the current plan raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="growth", daily_message_limit=300)
    db = AsyncMock()

    with pytest.raises(ValueError, match="not an upgrade"):
        await plan_service.upgrade_plan(db, client, "growth")


async def test_downgrade_raises():
    """Attempting a downgrade raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="pro", daily_message_limit=700)
    db = AsyncMock()

    with pytest.raises(ValueError, match="not an upgrade"):
        await plan_service.upgrade_plan(db, client, "starter")


async def test_upgrade_unknown_plan_raises():
    """Upgrading to an unknown slug raises ValueError."""
    client = Client(id=1, email="x@y.com", hashed_password="h", plan_slug="starter", daily_message_limit=100)
    db = AsyncMock()

    with pytest.raises(ValueError, match="Unknown plan"):
        await plan_service.upgrade_plan(db, client, "enterprise")


# ── plans router tests ────────────────────────────────────────────────────────

def test_list_plans_endpoint_returns_200(client):
    """GET /plans returns 200 with three plans (no auth required)."""
    response = client.get("/plans")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    slugs = [p["slug"] for p in data]
    assert slugs == ["starter", "growth", "pro"]


def test_list_plans_endpoint_includes_price_and_channels(client):
    """GET /plans includes price_inr and channels for each plan."""
    response = client.get("/plans")
    assert response.status_code == 200
    starter = next(p for p in response.json() if p["slug"] == "starter")
    assert starter["price_inr"] == 999
    assert starter["channels"] == ["whatsapp"]


def test_get_current_plan_returns_starter(client, mock_db, mock_settings):
    """GET /plans/current returns starter for a new client."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter",
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.get("/plans/current", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["slug"] == "starter"
    assert response.json()["price_inr"] == 999


def test_get_current_plan_requires_auth(client):
    """GET /plans/current returns 401 without a token."""
    response = client.get("/plans/current")
    assert response.status_code == 401


def test_upgrade_plan_returns_200(client, mock_db, mock_settings):
    """POST /plans/upgrade from starter to growth returns 200 with upgrade details."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter", daily_message_limit=100,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
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


def test_upgrade_plan_downgrade_returns_400(client, mock_db, mock_settings):
    """POST /plans/upgrade with a downgrade returns 400."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="pro", daily_message_limit=700,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/plans/upgrade",
        json={"plan_slug": "starter"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


def test_upgrade_plan_unknown_slug_returns_400(client, mock_db, mock_settings):
    """POST /plans/upgrade with an unknown plan slug returns 400."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, plan_slug="starter", daily_message_limit=100,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/plans/upgrade",
        json={"plan_slug": "ultra"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


def test_upgrade_plan_requires_auth(client):
    """POST /plans/upgrade returns 401 without a token."""
    response = client.post("/plans/upgrade", json={"plan_slug": "growth"})
    assert response.status_code == 401


# ── Instagram plan guard tests ────────────────────────────────────────────────

def test_instagram_webhook_blocked_on_starter(client, mock_db):
    """POST /instagram returns plan_restricted when client is on starter plan."""
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


def test_instagram_webhook_allowed_on_growth(client, mock_db):
    """POST /instagram proceeds past the plan guard on growth plan."""
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


def test_instagram_webhook_allowed_on_pro(client, mock_db):
    """POST /instagram proceeds past the plan guard on pro plan."""
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
