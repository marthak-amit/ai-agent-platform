"""
Tests for app/routers/team.py — Owner-only invite + permission-checklist admin.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.models.client import Client
from app.models.user import User
from app.services import auth_service


def _make_owner(client_id: int = 1) -> tuple[Client, User]:
    """Build an in-memory Client + its Owner User."""
    client = Client(id=client_id, email="owner@biz.com", hashed_password="h", is_active=True)
    owner = User(
        id=1, client_id=client_id, email="owner@biz.com", hashed_password="h",
        role="owner", permissions=[], is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    owner.client = client
    return client, owner


def _make_staff(client_id: int = 1, user_id: int = 2) -> User:
    """Build an in-memory non-owner User, wired to the same business as _make_owner."""
    client = Client(id=client_id, email="owner@biz.com", hashed_password="h", is_active=True)
    staff = User(
        id=user_id, client_id=client_id, email="staff@biz.com", hashed_password="h",
        role="staff", permissions=["order_view"], is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    staff.client = client
    return staff


# --- GET /team ---


def test_list_team_returns_members(client, mock_db, mock_settings):
    """GET /team lists every User for the Owner's business."""
    _, owner = _make_owner()
    staff = _make_staff()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [owner, staff]
    mock_db.execute = AsyncMock(side_effect=[auth_result, list_result])

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.get("/team", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["role"] == "owner"
    assert data[1]["role"] == "staff"
    assert data[1]["is_pending"] is False


def test_list_team_forbidden_for_non_owner(client, mock_db, mock_settings):
    """GET /team 403s when called by a non-owner."""
    staff = _make_staff()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = staff
    mock_db.execute.return_value = auth_result

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    response = client.get("/team", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


# --- POST /team/invite ---


def test_invite_creates_pending_user_and_returns_link(client, mock_db, mock_settings):
    """POST /team/invite creates a pending User and returns a copyable invite link."""
    _, owner = _make_owner()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    no_client_result = MagicMock()
    no_client_result.scalar_one_or_none.return_value = None
    no_user_result = MagicMock()
    no_user_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[auth_result, no_client_result, no_user_result])
    mock_db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", 42))

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.post(
        "/team/invite",
        json={"email": "manager@biz.com", "role": "manager"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    invite_link = response.json()["invite_link"]
    assert "/accept-invite?token=" in invite_link


def test_invite_rejects_invalid_role(client, mock_db, mock_settings):
    """POST /team/invite 422s for a role outside manager/staff."""
    _, owner = _make_owner()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    mock_db.execute.return_value = auth_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.post(
        "/team/invite",
        json={"email": "x@biz.com", "role": "owner"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_invite_rejects_duplicate_email(client, mock_db, mock_settings):
    """POST /team/invite 409s when the email already belongs to a User."""
    _, owner = _make_owner()
    existing_user = _make_staff()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    no_client_result = MagicMock()
    no_client_result.scalar_one_or_none.return_value = None
    existing_user_result = MagicMock()
    existing_user_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute = AsyncMock(side_effect=[auth_result, no_client_result, existing_user_result])

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.post(
        "/team/invite",
        json={"email": "staff@biz.com", "role": "staff"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


def test_invite_forbidden_for_non_owner(client, mock_db, mock_settings):
    """POST /team/invite 403s when called by a non-owner."""
    staff = _make_staff()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = staff
    mock_db.execute.return_value = auth_result

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    response = client.post(
        "/team/invite",
        json={"email": "x@biz.com", "role": "staff"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


# --- PATCH /team/{user_id} ---


def test_update_team_member_updates_permissions(client, mock_db, mock_settings):
    """PATCH /team/{id} updates a team member's permissions checklist."""
    _, owner = _make_owner()
    staff = _make_staff()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    member_result = MagicMock()
    member_result.scalar_one_or_none.return_value = staff
    mock_db.execute = AsyncMock(side_effect=[auth_result, member_result])

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.patch(
        f"/team/{staff.id}",
        json={"permissions": ["order_view", "mark_packed"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["permissions"] == ["order_view", "mark_packed"]


def test_update_team_member_rejects_unknown_permission_key(client, mock_db, mock_settings):
    """PATCH /team/{id} 422s for a permission key outside PERMISSION_KEYS."""
    _, owner = _make_owner()
    staff = _make_staff()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    member_result = MagicMock()
    member_result.scalar_one_or_none.return_value = staff
    mock_db.execute = AsyncMock(side_effect=[auth_result, member_result])

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.patch(
        f"/team/{staff.id}",
        json={"permissions": ["delete_everything"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_update_team_member_rejects_self_edit(client, mock_db, mock_settings):
    """PATCH /team/{id} 400s when the Owner targets their own row."""
    _, owner = _make_owner()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    mock_db.execute.return_value = auth_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.patch(
        f"/team/{owner.id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


def test_update_team_member_forbidden_for_non_owner(client, mock_db, mock_settings):
    """PATCH /team/{id} 403s when called by a non-owner."""
    staff = _make_staff()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = staff
    mock_db.execute.return_value = auth_result

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    response = client.patch(
        "/team/99",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_update_team_member_not_found_returns_404(client, mock_db, mock_settings):
    """PATCH /team/{id} 404s when no such team member exists for this business."""
    _, owner = _make_owner()
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = owner
    missing_result = MagicMock()
    missing_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[auth_result, missing_result])

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.patch(
        "/team/999",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


# --- POST /team/accept-invite ---


def test_accept_invite_sets_password_and_returns_token(client, mock_db, mock_settings):
    """POST /team/accept-invite sets the password and signs the invitee in."""
    pending = User(
        id=5, client_id=1, email="new@biz.com", hashed_password=None,
        role="staff", permissions=["order_view"], is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = pending
    mock_db.execute.return_value = mock_result

    invite_token = auth_service.create_access_token(
        {"sub": "new@biz.com", "purpose": "invite", "user_id": 5}
    )
    response = client.post(
        "/team/accept-invite",
        json={"token": invite_token, "password": "newpass123"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert pending.hashed_password is not None


def test_accept_invite_rejects_already_accepted(client, mock_db, mock_settings):
    """POST /team/accept-invite 400s if the invite was already used."""
    already_set = User(
        id=5, client_id=1, email="new@biz.com", hashed_password="already-set",
        role="staff", permissions=[], is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = already_set
    mock_db.execute.return_value = mock_result

    invite_token = auth_service.create_access_token(
        {"sub": "new@biz.com", "purpose": "invite", "user_id": 5}
    )
    response = client.post(
        "/team/accept-invite",
        json={"token": invite_token, "password": "newpass123"},
    )
    assert response.status_code == 400


def test_accept_invite_rejects_non_invite_token(client, mock_db, mock_settings):
    """POST /team/accept-invite 400s for a normal (non-invite) access token."""
    login_token = auth_service.create_access_token({"sub": "new@biz.com"})
    response = client.post(
        "/team/accept-invite",
        json={"token": login_token, "password": "newpass123"},
    )
    assert response.status_code == 400


def test_accept_invite_rejects_garbage_token(client, mock_db, mock_settings):
    """POST /team/accept-invite 400s for an invalid token string."""
    response = client.post(
        "/team/accept-invite",
        json={"token": "not-a-real-token", "password": "newpass123"},
    )
    assert response.status_code == 400
