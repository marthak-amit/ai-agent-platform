"""
Tests for app/services/admin_service.py and app/routers/admin.py.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.client import Client
from app.models.usage_log import UsageLog
from app.services import admin_service

ADMIN_KEY = "test-admin-key"
HEADERS = {"X-Admin-Key": ADMIN_KEY}


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_client(**kwargs) -> Client:
    """Return a Client with sensible defaults."""
    defaults = dict(
        id=1, email="owner@biz.com", business_name="Test Biz",
        hashed_password="h", is_active=True, plan_slug="starter",
        daily_message_limit=100, created_at=datetime(2026, 1, 1),
    )
    defaults.update(kwargs)
    return Client(**defaults)


# ── admin_service unit tests ──────────────────────────────────────────────────

async def test_get_all_clients_builds_correct_dicts():
    """get_all_clients returns one dict per client, revenue from plan_price_snapshot."""
    clients = [
        _make_client(id=1, plan_slug="starter", plan_price_snapshot=1499),
        _make_client(id=2, email="other@biz.com", plan_slug="growth", plan_price_snapshot=3999),
    ]

    db = AsyncMock()
    # execute call 1: clients
    clients_result = MagicMock()
    clients_result.scalars.return_value.all.return_value = clients
    # execute call 2: today usage
    today_result = MagicMock()
    today_result.__iter__ = lambda s: iter([
        MagicMock(client_id=1, message_count=10),
    ])
    # execute call 3: monthly usage
    monthly_result = MagicMock()
    monthly_result.__iter__ = lambda s: iter([
        MagicMock(client_id=1, total=250),
        MagicMock(client_id=2, total=80),
    ])
    db.execute = AsyncMock(side_effect=[clients_result, today_result, monthly_result])

    rows = await admin_service.get_all_clients(db)

    assert len(rows) == 2
    c1 = next(r for r in rows if r["id"] == 1)
    assert c1["messages_today"] == 10
    assert c1["messages_this_month"] == 250
    assert c1["monthly_revenue_inr"] == 1499  # starter's snapshot

    c2 = next(r for r in rows if r["id"] == 2)
    assert c2["messages_today"] == 0          # not in today_map
    assert c2["monthly_revenue_inr"] == 3999  # growth's snapshot


async def test_get_platform_stats_returns_correct_structure():
    """get_platform_stats aggregates queries into the expected shape."""
    db = AsyncMock()

    active_r = MagicMock(); active_r.scalar.return_value = 5
    # sum of active clients' plan_price_snapshot
    revenue_r = MagicMock(); revenue_r.scalar.return_value = 6995
    today_r = MagicMock(); today_r.scalar.return_value = 120
    month_r = MagicMock(); month_r.scalar.return_value = 3400

    db.execute = AsyncMock(side_effect=[active_r, revenue_r, today_r, month_r])

    stats = await admin_service.get_platform_stats(db)

    assert stats["active_clients"] == 5
    assert stats["monthly_revenue_inr"] == 6995
    assert stats["messages_today"] == 120
    assert stats["messages_this_month"] == 3400


async def test_set_client_active_suspends():
    """set_client_active(False) sets is_active=False and commits."""
    c = _make_client(is_active=True)
    db = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = c
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    updated = await admin_service.set_client_active(db, 1, active=False)

    assert updated.is_active is False
    db.commit.assert_called_once()


async def test_set_client_active_activates():
    """set_client_active(True) sets is_active=True and commits."""
    c = _make_client(is_active=False)
    db = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = c
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    updated = await admin_service.set_client_active(db, 1, active=True)

    assert updated.is_active is True


async def test_set_client_active_not_found_raises():
    """set_client_active raises ValueError for unknown client_id."""
    db = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    with pytest.raises(ValueError, match="not found"):
        await admin_service.set_client_active(db, 999, active=False)


async def test_get_revenue_breakdown_includes_all_tiers(seeded_plans):
    """get_revenue_breakdown lists all three plan tiers even with zero clients."""
    db = AsyncMock()
    dist_r = MagicMock()
    dist_r.__iter__ = lambda s: iter([("starter", 4, 4 * 1499)])  # only starter has clients
    db.execute = AsyncMock(return_value=dist_r)

    result = await admin_service.get_revenue_breakdown(db)

    assert len(result["breakdown"]) == 3
    slugs = [row["plan"] for row in result["breakdown"]]
    assert slugs == ["starter", "growth", "pro"]

    starter_row = result["breakdown"][0]
    assert starter_row["client_count"] == 4
    assert starter_row["revenue_inr"] == 4 * 1499

    growth_row = result["breakdown"][1]
    assert growth_row["client_count"] == 0
    assert growth_row["revenue_inr"] == 0


async def test_get_revenue_breakdown_total(seeded_plans):
    """total_revenue_inr sums across all tiers correctly, from plan_price_snapshot."""
    db = AsyncMock()
    dist_r = MagicMock()
    dist_r.__iter__ = lambda s: iter([
        ("starter", 2, 2 * 1499), ("growth", 3, 3 * 3999), ("pro", 1, 1 * 9999),
    ])
    db.execute = AsyncMock(return_value=dist_r)

    result = await admin_service.get_revenue_breakdown(db)

    expected = 2 * 1499 + 3 * 3999 + 1 * 9999
    assert result["total_revenue_inr"] == expected


# ── router auth tests ─────────────────────────────────────────────────────────

def test_admin_endpoints_require_key(client):
    """All admin endpoints return 401 without X-Admin-Key header."""
    for method, path in [
        ("GET", "/admin/clients"),
        ("GET", "/admin/stats"),
        ("PUT", "/admin/clients/1/suspend"),
        ("PUT", "/admin/clients/1/activate"),
        ("GET", "/admin/revenue"),
        ("GET", "/admin/plans"),
    ]:
        response = getattr(client, method.lower())(path)
        assert response.status_code == 401, f"{method} {path} should be 401 without key"


def test_admin_endpoints_reject_wrong_key(client):
    """All admin endpoints return 401 with a wrong X-Admin-Key."""
    bad = {"X-Admin-Key": "totally-wrong"}
    for method, path in [
        ("GET", "/admin/clients"),
        ("GET", "/admin/stats"),
        ("GET", "/admin/revenue"),
    ]:
        response = getattr(client, method.lower())(path, headers=bad)
        assert response.status_code == 401, f"{method} {path} should be 401 with wrong key"


# ── GET /admin/clients ────────────────────────────────────────────────────────

def test_list_clients_returns_200(client, mock_db, mock_settings):
    """GET /admin/clients returns 200 with admin key."""
    fake_rows = [
        {
            "id": 1, "email": "a@b.com", "business_name": "Biz",
            "plan_slug": "starter", "is_active": True,
            "messages_today": 5, "messages_this_month": 100,
            "monthly_revenue_inr": 999, "created_at": datetime(2026, 1, 1),
        }
    ]
    with patch(
        "app.services.admin_service.get_all_clients",
        new=AsyncMock(return_value=fake_rows),
    ):
        response = client.get("/admin/clients", headers=HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["email"] == "a@b.com"
    assert data[0]["monthly_revenue_inr"] == 999


# ── GET /admin/stats ──────────────────────────────────────────────────────────

def test_platform_stats_returns_200(client, mock_db, mock_settings):
    """GET /admin/stats returns 200 with the stats dict."""
    fake_stats = {
        "active_clients": 12,
        "monthly_revenue_inr": 24000,
        "messages_today": 500,
        "messages_this_month": 14000,
    }
    with patch(
        "app.services.admin_service.get_platform_stats",
        new=AsyncMock(return_value=fake_stats),
    ):
        response = client.get("/admin/stats", headers=HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["active_clients"] == 12
    assert data["monthly_revenue_inr"] == 24000


# ── PUT /admin/clients/{id}/suspend ──────────────────────────────────────────

def test_suspend_client_returns_200(client, mock_db, mock_settings):
    """PUT /admin/clients/1/suspend returns 200 with is_active=false."""
    suspended = _make_client(id=1, is_active=False)
    with patch(
        "app.services.admin_service.set_client_active",
        new=AsyncMock(return_value=suspended),
    ):
        response = client.put("/admin/clients/1/suspend", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert "suspended" in response.json()["message"]


def test_suspend_nonexistent_client_returns_404(client, mock_db, mock_settings):
    """PUT /admin/clients/999/suspend returns 404 for unknown client."""
    with patch(
        "app.services.admin_service.set_client_active",
        new=AsyncMock(side_effect=ValueError("Client 999 not found.")),
    ):
        response = client.put("/admin/clients/999/suspend", headers=HEADERS)

    assert response.status_code == 404


# ── PUT /admin/clients/{id}/activate ─────────────────────────────────────────

def test_activate_client_returns_200(client, mock_db, mock_settings):
    """PUT /admin/clients/1/activate returns 200 with is_active=true."""
    activated = _make_client(id=1, is_active=True)
    with patch(
        "app.services.admin_service.set_client_active",
        new=AsyncMock(return_value=activated),
    ):
        response = client.put("/admin/clients/1/activate", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert "activated" in response.json()["message"]


# ── GET /admin/revenue ────────────────────────────────────────────────────────

def test_revenue_breakdown_returns_200(client, mock_db, mock_settings):
    """GET /admin/revenue returns month, total, and three-tier breakdown."""
    fake_revenue = {
        "month": "2026-05",
        "total_revenue_inr": 6995,
        "breakdown": [
            {"plan": "starter", "plan_name": "Starter", "client_count": 3, "revenue_inr": 2997},
            {"plan": "growth",  "plan_name": "Growth",  "client_count": 2, "revenue_inr": 3998},
            {"plan": "pro",     "plan_name": "Pro",     "client_count": 0, "revenue_inr": 0},
        ],
    }
    with patch(
        "app.services.admin_service.get_revenue_breakdown",
        new=AsyncMock(return_value=fake_revenue),
    ):
        response = client.get("/admin/revenue", headers=HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["month"] == "2026-05"
    assert data["total_revenue_inr"] == 6995
    assert len(data["breakdown"]) == 3
    assert data["breakdown"][0]["plan"] == "starter"


# ── GET /admin/plans ──────────────────────────────────────────────────────────

def test_list_all_plans_returns_200(client, mock_db, mock_settings, seeded_plans):
    """GET /admin/plans returns all 3 seeded plans, in tier order."""
    response = client.get("/admin/plans", headers=HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert [p["plan_id"] for p in data] == ["starter", "growth", "pro"]
    assert data[0]["price_inr"] == 1499


def test_list_all_plans_rejects_wrong_key(client, mock_db, mock_settings, seeded_plans):
    """GET /admin/plans returns 401 with a wrong X-Admin-Key."""
    response = client.get("/admin/plans", headers={"X-Admin-Key": "wrong"})
    assert response.status_code == 401


# ── PUT /admin/plans/{plan_id} ────────────────────────────────────────────────

def test_update_plan_returns_200_and_updates_value(client, mock_db, mock_settings, seeded_plans):
    """PUT /admin/plans/starter updates price_inr and reflects it in the response."""
    from app.models.plan import Plan

    updated_plan = Plan(
        plan_id="starter", name="Starter", price_inr=1799, conv_limit=700,
        image_quota=20, image_overage_price=8, daily_msg_limit=100,
        channels=["whatsapp"], campaign_allowed=False, campaign_max_recipients=0,
        campaign_monthly_limit=0, tier_order=0, description="Starter plan.",
        is_active=True,
    )
    with patch(
        "app.services.admin_service.update_plan",
        new=AsyncMock(return_value=updated_plan),
    ):
        response = client.put(
            "/admin/plans/starter", json={"price_inr": 1799}, headers=HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["price_inr"] == 1799


def test_update_plan_unknown_returns_404(client, mock_db, mock_settings, seeded_plans):
    """PUT /admin/plans/{unknown} returns 404."""
    with patch(
        "app.services.admin_service.update_plan",
        new=AsyncMock(side_effect=ValueError("Plan 'enterprise' not found.")),
    ):
        response = client.put(
            "/admin/plans/enterprise", json={"price_inr": 999}, headers=HEADERS,
        )

    assert response.status_code == 404


def test_update_plan_rejects_negative_price(client, mock_db, mock_settings, seeded_plans):
    """PUT /admin/plans/starter with a negative price_inr fails validation."""
    response = client.put(
        "/admin/plans/starter", json={"price_inr": -100}, headers=HEADERS,
    )
    assert response.status_code == 422


def test_update_plan_requires_admin_key(client, mock_db, mock_settings, seeded_plans):
    """PUT /admin/plans/starter returns 401 without X-Admin-Key."""
    response = client.put("/admin/plans/starter", json={"price_inr": 1799})
    assert response.status_code == 401


async def test_update_plan_invalidates_cache(seeded_plans):
    """admin_service.update_plan clears the plan_cache after committing."""
    from app.models.plan import Plan
    from app.services import plan_cache

    plan = Plan(
        plan_id="starter", name="Starter", price_inr=1499, conv_limit=700,
        image_quota=20, image_overage_price=8, daily_msg_limit=100,
        channels=["whatsapp"], campaign_allowed=False, campaign_max_recipients=0,
        campaign_monthly_limit=0, tier_order=0, description="Starter plan.",
        is_active=True,
    )
    db = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = plan
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    # Cache is warm (via seeded_plans); after update_plan it must be cleared.
    assert plan_cache._loaded_at is not None

    await admin_service.update_plan(db, "starter", {"price_inr": 1599})

    assert plan.price_inr == 1599
    assert plan_cache._loaded_at is None


async def test_update_plan_unknown_plan_raises():
    """admin_service.update_plan raises ValueError for an unknown plan_id."""
    db = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    with pytest.raises(ValueError, match="not found"):
        await admin_service.update_plan(db, "enterprise", {"price_inr": 999})
