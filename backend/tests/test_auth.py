"""
Tests for app/services/auth_service.py and app/routers/auth.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import auth_service


# --- auth_service unit tests ---


def test_hash_and_verify_password(mock_settings):
    """hash_password and verify_password round-trip correctly."""
    hashed = auth_service.hash_password("secret123")
    assert hashed != "secret123"
    assert auth_service.verify_password("secret123", hashed) is True
    assert auth_service.verify_password("wrong", hashed) is False


def test_create_access_token_is_decodable(mock_settings):
    """Token created by create_access_token decodes to the original payload."""
    from jose import jwt

    token = auth_service.create_access_token({"sub": "test@example.com"})
    payload = jwt.decode(token, "test-secret-key", algorithms=["HS256"])
    assert payload["sub"] == "test@example.com"


async def test_get_current_client_valid_token(mock_settings):
    """get_current_client returns a Client for a valid token."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "test@example.com"})
    client = Client(id=1, email="test@example.com", hashed_password="x", is_active=True)

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = client
    db.execute.return_value = mock_result

    result = await auth_service.get_current_client(token, db)
    assert result.email == "test@example.com"


async def test_get_current_client_invalid_token(mock_settings):
    """get_current_client raises ValueError for a bad token."""
    db = AsyncMock()
    with pytest.raises(ValueError, match="Invalid token"):
        await auth_service.get_current_client("not.a.jwt", db)


# --- auth router tests ---


def test_register_creates_client(client, mock_db):
    """POST /auth/register creates a new client and returns 201."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=_set_id)

    response = client.post(
        "/auth/register",
        json={
            "email": "owner@biz.com",
            "password": "pass123",
            "business_name": "Test Biz",
            "phone": "+919876543210",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "owner@biz.com"
    assert data["business_name"] == "Test Biz"


def test_register_without_phone(client, mock_db):
    """POST /auth/register succeeds when phone is omitted."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=_set_id)

    response = client.post(
        "/auth/register",
        json={"email": "owner@biz.com", "password": "pass123", "business_name": "Test Biz"},
    )
    assert response.status_code == 201


def test_register_duplicate_email_returns_409(client, mock_db):
    """POST /auth/register returns 409 if email already exists."""
    from app.models.client import Client

    existing = Client(id=1, email="owner@biz.com", hashed_password="x")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/auth/register",
        json={"email": "owner@biz.com", "password": "pass123", "business_name": "Biz"},
    )
    assert response.status_code == 409


def test_login_valid_credentials(client, mock_db):
    """POST /auth/login returns a JWT for valid credentials."""
    from app.models.client import Client

    hashed = auth_service.hash_password("pass123")
    existing = Client(id=1, email="owner@biz.com", hashed_password=hashed, is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/auth/login",
        json={"email": "owner@biz.com", "password": "pass123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_invalid_credentials(client, mock_db):
    """POST /auth/login returns 401 for wrong password."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/auth/login",
        json={"email": "x@y.com", "password": "wrong"},
    )
    assert response.status_code == 401


def test_get_me_returns_profile(client, mock_db, mock_settings):
    """GET /auth/me returns the current client profile."""
    from app.models.client import Client, DEFAULT_SYSTEM_PROMPT

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1,
        email="owner@biz.com",
        hashed_password="x",
        business_name="My Biz",
        phone=None,
        gemini_system_prompt=DEFAULT_SYSTEM_PROMPT,
        is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "owner@biz.com"
    assert data["business_name"] == "My Biz"


def test_get_me_unauthenticated(client):
    """GET /auth/me returns 401 without a token."""
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_logout_returns_200(client, mock_db, mock_settings):
    """POST /auth/logout returns 200 with a valid token."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["message"] == "Logged out successfully."


# --- helpers ---

async def _set_id(obj):
    """Side effect to simulate DB setting an id on a new model."""
    obj.id = 1
    obj.business_name = obj.business_name or ""
    from app.models.client import DEFAULT_SYSTEM_PROMPT

    obj.gemini_system_prompt = DEFAULT_SYSTEM_PROMPT
