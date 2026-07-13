"""
Tests for the permission-checklist dependencies in app/routers/auth.py and
the User model helpers in app/models/user.py.

These call the dependency functions directly with an in-memory User (no DB
mocking needed) since get_owner_client / require_permission only branch on
the already-resolved User passed in by FastAPI's Depends chain.
"""

import pytest
from fastapi import HTTPException

from app.models.client import Client
from app.models.user import User
from app.routers.auth import get_owner_client, require_permission


def _make_user(role: str = "staff", permissions: list[str] | None = None) -> User:
    """Build an in-memory User with its Client relationship set."""
    client = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    user = User(
        id=2, client_id=1, email="x@y.com", hashed_password="x",
        role=role, permissions=permissions or [], is_active=True,
    )
    user.client = client
    return user


# --- User model unit tests ---


def test_user_is_owner_true_for_owner_role():
    """is_owner is True exactly when role == 'owner'."""
    assert _make_user(role="owner").is_owner is True


def test_user_is_owner_false_for_non_owner_roles():
    """is_owner is False for manager and staff roles."""
    assert _make_user(role="manager").is_owner is False
    assert _make_user(role="staff").is_owner is False


def test_user_has_permission_owner_bypasses_checklist():
    """An owner has every permission regardless of the permissions array."""
    owner = _make_user(role="owner", permissions=[])
    assert owner.has_permission("catalog_edit") is True
    assert owner.has_permission("anything_not_a_real_key") is True


def test_user_has_permission_checks_checklist_for_non_owner():
    """A non-owner only has permissions present in their checklist."""
    staff = _make_user(role="staff", permissions=["order_view"])
    assert staff.has_permission("order_view") is True
    assert staff.has_permission("catalog_edit") is False


# --- get_owner_client dependency ---


async def test_get_owner_client_allows_owner():
    """get_owner_client returns the Client for an Owner user."""
    owner = _make_user(role="owner")
    result = await get_owner_client(owner)
    assert result is owner.client


async def test_get_owner_client_rejects_manager():
    """get_owner_client 403s a manager, regardless of their permissions checklist."""
    manager = _make_user(role="manager", permissions=["catalog_edit", "order_view"])
    with pytest.raises(HTTPException) as exc_info:
        await get_owner_client(manager)
    assert exc_info.value.status_code == 403


async def test_get_owner_client_rejects_staff():
    """get_owner_client 403s a staff user."""
    staff = _make_user(role="staff")
    with pytest.raises(HTTPException) as exc_info:
        await get_owner_client(staff)
    assert exc_info.value.status_code == 403


# --- require_permission dependency factory ---


async def test_require_permission_owner_bypasses_checklist():
    """require_permission always passes for the Owner, even with an empty checklist."""
    owner = _make_user(role="owner", permissions=[])
    checker = require_permission("catalog_edit")
    result = await checker(owner)
    assert result is owner


async def test_require_permission_allows_when_key_present():
    """require_permission passes a non-owner who holds the required key."""
    staff = _make_user(role="staff", permissions=["order_view", "manual_reply"])
    checker = require_permission("order_view")
    result = await checker(staff)
    assert result is staff


async def test_require_permission_denies_when_key_missing():
    """require_permission 403s a non-owner missing the required key."""
    staff = _make_user(role="staff", permissions=["order_view"])
    checker = require_permission("catalog_edit")
    with pytest.raises(HTTPException) as exc_info:
        await checker(staff)
    assert exc_info.value.status_code == 403
