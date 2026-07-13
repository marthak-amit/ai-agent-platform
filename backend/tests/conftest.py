"""
Shared pytest fixtures for all test modules.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings, get_settings
from app.db import get_db


@pytest.fixture
def mock_settings(monkeypatch):
    """
    Override Settings with test values and clear the lru_cache.

    Yields a Settings instance with deterministic test secrets.
    """
    test_settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        gemini_api_key="test-gemini-key",
        openai_api_key="test-openai-key",
        groq_api_key="test-groq-key",
        meta_app_secret="test-app-secret",
        meta_verify_token="test-verify-token",
        whatsapp_access_token="test-wa-token",
        whatsapp_phone_number_id="1234567890",
        instagram_access_token="test-ig-token",
        razorpay_key_id="test-rzp-key",
        razorpay_key_secret="test-rzp-secret",
        secret_key="test-secret-key",
        admin_secret_key="test-admin-key",
        meta_app_id="test-meta-app-id",
        meta_oauth_redirect_uri="https://api.test.example/integrations/instagram/callback",
    )
    get_settings.cache_clear()
    monkeypatch.setattr("app.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.webhook.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.instagram.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.integrations.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.admin.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.team.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.gemini_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.whatsapp_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.instagram_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.razorpay_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.auth_service.get_settings", lambda: test_settings)
    yield test_settings
    get_settings.cache_clear()


@pytest.fixture
def mock_db():
    """Return an AsyncMock that mimics an AsyncSession."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def make_test_user():
    """
    Factory fixture: wrap a Client in an in-memory User for auth-lookup mocks.

    Since login/get_current_client now resolve through the users table (not
    clients directly), any test that mocks the auth dependency's DB query
    must return a User (with `.client` set), not a bare Client — a Client
    instance has no `.client` attribute and no `.is_owner`/`.has_permission`.
    Use like: `mock_result.scalar_one_or_none.return_value = make_test_user(existing_client)`.
    """

    def _make(client, role: str = "owner", permissions: list[str] | None = None, user_id: int = 1):
        from app.models.user import User

        user = User(
            id=user_id,
            client_id=client.id,
            email=client.email,
            hashed_password=client.hashed_password,
            role=role,
            permissions=permissions if permissions is not None else [],
            is_active=True,
        )
        user.client = client
        return user

    return _make


@pytest.fixture
def client(mock_settings, mock_db):
    """
    Return a TestClient with get_db overridden to inject mock_db.

    Ensures no real DB connection is attempted during route tests.
    """
    from app.main import app

    async def _override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_db, None)
