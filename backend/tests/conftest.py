"""
Shared pytest fixtures for all test modules.
"""

import os
os.environ.setdefault("USD_INR", "83")   # USD_INR is a required setting (no default in code); tests pin a rate
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings, get_settings
from app.db import get_db
from app.services import plan_cache


@pytest.fixture(autouse=True)
def _reset_plan_cache():
    """Clear the in-process plan cache before/after every test (it's a module global)."""
    plan_cache.invalidate()
    yield
    plan_cache.invalidate()


@pytest.fixture(autouse=True)
def _no_llm_usage_persist(monkeypatch):
    """
    Never write llm_usage rows from tests (they would otherwise open a real DB session via
    llm_call). tests/test_llm_usage.py re-patches _persist_row itself to capture/verify rows.
    """
    monkeypatch.setattr("app.services.llm_usage_service._persist_row", AsyncMock())
    yield


@pytest.fixture(autouse=True)
def _router_v2_off_by_default(monkeypatch):
    """
    ROUTER_V2 is ON for client_id=1 in production, and every replay database creates client 1 —
    so without this the whole legacy suite would silently run through the LLM router. Tests that
    exercise the router opt in per client via Client.router_v2_enabled=True (a DB column that
    overrides the env default), never via this env var.
    """
    from app.config import get_settings

    monkeypatch.setenv("ROUTER_V2_CLIENT_IDS", "")
    # Same for agent v3 (also ON for client 1 by default): the legacy suite must keep running the legacy pipeline.
    # Agent-v3 tests opt in per client via Client.agent_v3_enabled=True.
    monkeypatch.setenv("AGENT_V3_CLIENT_IDS", "")
    # The webhook answers Meta first and processes in a background task in production; the legacy suite posts a message and
    # then reads the reply, so it runs inline. Background-mode tests switch it on explicitly.
    monkeypatch.setenv("WEBHOOK_BACKGROUND_PROCESSING", "false")
    # The client-side Groq token bucket (llm_rate_limiter) is production protection against the provider's 8k TPM cap; the suite's scripted
    # / fake LLMs answer instantly and must not be throttled by it. tests/test_v16_units.py switches the default limits back on for its own tests.
    monkeypatch.setenv("LLM_RATE_LIMITS", "{}")
    # Message batching adds a quiet-window wait to every text turn; the suite sends one message at a time and
    # expects the reply immediately. The batching tests switch it on explicitly.
    monkeypatch.setenv("MESSAGE_BATCH_WINDOW_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _NullSavepoint:
    """Async context manager standing in for AsyncSession.begin_nested() on mocked sessions."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def with_savepoint(db):
    """Give a mocked AsyncSession a working begin_nested() (add_or_get inserts inside a SAVEPOINT); returns db."""
    db.begin_nested = MagicMock(return_value=_NullSavepoint())
    return db


def make_test_plans() -> list:
    """Build the 3 seeded Plan ORM rows in-memory, matching migration 0053's seed data."""
    from app.models.plan import Plan

    return [
        Plan(
            plan_id="starter", name="Starter", price_inr=1499, conv_limit=700,
            image_quota=20, image_overage_price=8, daily_msg_limit=100,
            channels=["whatsapp"], campaign_allowed=False, campaign_max_recipients=0,
            campaign_monthly_limit=0, tier_order=0, description="Starter plan.",
            is_active=True,
        ),
        Plan(
            plan_id="growth", name="Growth", price_inr=3999, conv_limit=2000,
            image_quota=50, image_overage_price=6, daily_msg_limit=300,
            channels=["whatsapp", "instagram"], campaign_allowed=True,
            campaign_max_recipients=500, campaign_monthly_limit=1, tier_order=1,
            description="Growth plan.", is_active=True,
        ),
        Plan(
            plan_id="pro", name="Pro", price_inr=9999, conv_limit=6000,
            image_quota=100, image_overage_price=5, daily_msg_limit=700,
            channels=["whatsapp", "instagram", "website"], campaign_allowed=True,
            campaign_max_recipients=99999, campaign_monthly_limit=99999, tier_order=2,
            description="Pro plan.", is_active=True,
        ),
    ]


@pytest.fixture
def seeded_plans():
    """
    Pre-populate the plan_cache module cache directly, bypassing the DB.

    Any test exercising plan-dependent code (plan_service, billing_service,
    campaign_service guards, the /plans or /admin/plans routes) should
    request this fixture instead of mocking db.execute for plan lookups —
    plan_cache checks its own dict before ever calling db.execute, so
    pre-loading it here means those code paths never touch mock_db at all.
    """
    from datetime import datetime, timezone

    plans = make_test_plans()
    plan_cache._cache.clear()
    for p in plans:
        plan_cache._cache[p.plan_id] = plan_cache._plan_to_dict(p)
    plan_cache._loaded_at = datetime.now(timezone.utc)
    yield plan_cache._cache


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
    monkeypatch.setattr("app.routers.whatsapp_signup.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.admin_auth_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.team.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.gemini_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.whatsapp_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.llm_client.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.llm_usage_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.voice_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.vision_service.get_settings", lambda: test_settings)
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


RECEIPT_OCR_TEXT = (
    "Google Pay\nPayment successful\nPaid to Test Shop\nUPI transaction ID 412345678901\nBanking name TEST SHOP"
)


@pytest.fixture(autouse=True)
def _images_read_as_payment_receipts_by_default(monkeypatch):
    """
    Images sent while an order awaits payment are classified before they are filed (``payment_proof_classifier``: OCR → vision). The replay
    media stub serves a 1×1 PNG, which would read as a text-free product photo; the older replays mean "the customer sends their payment
    screenshot", so OCR returns a receipt unless a test overrides ``payment_proof_classifier.ocr_text`` itself.
    """
    async def _receipt(_image_bytes):
        """OCR text of a UPI receipt (no amount: the order total is not on it)."""
        return RECEIPT_OCR_TEXT

    async def _no_vision(*_a, **_k):
        """The vision model is never called by the replays (no network, no tokens)."""
        return None

    monkeypatch.setattr("app.services.payment_proof_classifier.ocr_text", _receipt)
    monkeypatch.setattr("app.services.payment_proof_classifier.vision_read", _no_vision)
