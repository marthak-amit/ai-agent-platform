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
    """get_current_client resolves token -> User -> its Client."""
    from app.models.client import Client
    from app.models.user import User

    token = auth_service.create_access_token({"sub": "test@example.com"})
    client = Client(id=1, email="test@example.com", hashed_password="x", is_active=True)
    user = User(id=1, client_id=1, email="test@example.com", hashed_password="x", role="owner", permissions=[], is_active=True)
    user.client = client

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    db.execute.return_value = mock_result

    result = await auth_service.get_current_client(token, db)
    assert result.email == "test@example.com"


async def test_get_current_client_invalid_token(mock_settings):
    """get_current_client raises ValueError for a bad token."""
    db = AsyncMock()
    with pytest.raises(ValueError, match="Invalid token"):
        await auth_service.get_current_client("not.a.jwt", db)


async def test_get_current_user_inactive_user_rejected(mock_settings):
    """get_current_user raises ValueError for a deactivated team member."""
    from app.models.client import Client
    from app.models.user import User

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    client = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    user = User(id=2, client_id=1, email="staff@biz.com", hashed_password="x", role="staff", permissions=[], is_active=False)
    user.client = client

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    db.execute.return_value = mock_result

    with pytest.raises(ValueError, match="not found or inactive"):
        await auth_service.get_current_user(token, db)


async def test_get_current_user_rejects_invite_token(mock_settings):
    """get_current_user refuses an invite-purpose token as an access token."""
    db = AsyncMock()
    invite_token = auth_service.create_access_token({"sub": "staff@biz.com", "purpose": "invite", "user_id": 2})
    with pytest.raises(ValueError, match="Invite tokens"):
        await auth_service.get_current_user(invite_token, db)


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


def test_register_creates_owner_user(client, mock_db):
    """POST /auth/register also creates the first (Owner) User for the business."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    mock_db.refresh = AsyncMock(side_effect=_set_id)

    response = client.post(
        "/auth/register",
        json={"email": "owner@biz.com", "password": "pass123", "business_name": "Test Biz"},
    )
    assert response.status_code == 201
    current_user = response.json()["current_user"]
    assert current_user["email"] == "owner@biz.com"
    assert current_user["role"] == "owner"
    assert current_user["is_owner"] is True


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


def test_login_valid_credentials(client, mock_db, make_test_user):
    """POST /auth/login returns a JWT for valid credentials."""
    from app.models.client import Client
    from app.models.user import User

    hashed = auth_service.hash_password("pass123")
    business = Client(id=1, email="owner@biz.com", hashed_password=hashed, is_active=True)
    existing = User(id=1, client_id=1, email="owner@biz.com", hashed_password=hashed, role="owner", permissions=[], is_active=True)
    existing.client = business
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


def test_login_pending_invite_rejected(client, mock_db):
    """POST /auth/login returns 401 when the invitee hasn't set a password yet."""
    from app.models.user import User

    pending = User(id=2, client_id=1, email="staff@biz.com", hashed_password=None, role="staff", permissions=[], is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = pending
    mock_db.execute.return_value = mock_result

    response = client.post(
        "/auth/login",
        json={"email": "staff@biz.com", "password": "whatever"},
    )
    assert response.status_code == 401


def test_get_me_returns_profile(client, mock_db, mock_settings, make_test_user):
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
        # Non-nullable fields with column-level defaults — must be set explicitly
        # since this Client is never flushed through a real DB session.
        hsn_code="5007",
        briefing_enabled=True,
        briefing_time="09:00",
        dashboard_language="en",
        catalogue_theme_color="#6366F1",
        accepts_cod=False,
        accepts_upi=True,
        accepts_bank_transfer=False,
        onboarding_step=0,
        onboarding_completed=False,
        plan_slug="starter",
        daily_message_limit=100,
        ig_comment_autoreply_enabled=False,
        ig_comment_reply_all=False,
        ig_comment_triggers=["price", "order", "want this", "how much", "available", "buy"],
        ig_comment_reply_text={"english": "Check your DM 👀", "hindi": "", "gujarati": ""},
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "owner@biz.com"
    assert data["business_name"] == "My Biz"
    assert data["current_user"]["role"] == "owner"
    assert data["current_user"]["is_owner"] is True


def test_get_me_unauthenticated(client):
    """GET /auth/me returns 401 without a token."""
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_get_me_never_leaks_raw_tokens(client, mock_db, mock_settings, make_test_user):
    """GET /auth/me must never include raw whatsapp/instagram access tokens."""
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
        whatsapp_access_token="super-secret-whatsapp-token",
        instagram_access_token="super-secret-instagram-token",
        instagram_account_id="178414581234567",
        hsn_code="5007",
        briefing_enabled=True,
        briefing_time="09:00",
        dashboard_language="en",
        catalogue_theme_color="#6366F1",
        accepts_cod=False,
        accepts_upi=True,
        accepts_bank_transfer=False,
        onboarding_step=0,
        onboarding_completed=False,
        plan_slug="starter",
        daily_message_limit=100,
        ig_comment_autoreply_enabled=False,
        ig_comment_reply_all=False,
        ig_comment_triggers=["price", "order", "want this", "how much", "available", "buy"],
        ig_comment_reply_text={"english": "Check your DM 👀", "hindi": "", "gujarati": ""},
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    body_text = response.text
    assert "super-secret-whatsapp-token" not in body_text
    assert "super-secret-instagram-token" not in body_text
    assert "whatsapp_access_token" not in data
    assert "instagram_access_token" not in data
    assert data["whatsapp_connected"] is True
    assert data["instagram_connected"] is True
    assert data["instagram_account_id"] == "178414581234567"


def test_logout_returns_200(client, mock_db, mock_settings, make_test_user):
    """POST /auth/logout returns 200 with a valid token."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["message"] == "Logged out successfully."


# --- update_me permission-gating tests ---


def test_update_me_owner_can_update_any_field(client, mock_db, mock_settings, make_test_user):
    """PATCH /auth/me lets the Owner update fields outside the comment-settings set."""
    from app.models.client import Client, DEFAULT_SYSTEM_PROMPT

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="x", business_name="Old Name",
        gemini_system_prompt=DEFAULT_SYSTEM_PROMPT, is_active=True, hsn_code="5007",
        briefing_enabled=True, briefing_time="09:00", dashboard_language="en",
        catalogue_theme_color="#6366F1", accepts_cod=False, accepts_upi=True,
        accepts_bank_transfer=False, onboarding_step=0, onboarding_completed=False,
        plan_slug="starter", daily_message_limit=100, ig_comment_autoreply_enabled=False,
        ig_comment_reply_all=False, ig_comment_triggers=[], ig_comment_reply_text={},
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing, role="owner")
    mock_db.execute.return_value = mock_result

    response = client.patch(
        "/auth/me",
        json={"business_name": "New Name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["business_name"] == "New Name"


def test_update_me_non_owner_forbidden_field_returns_403(client, mock_db, mock_settings, make_test_user):
    """PATCH /auth/me 403s a non-owner submitting a field outside comment_settings."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(
        existing, role="manager", permissions=["comment_settings"], user_id=2
    )
    mock_db.execute.return_value = mock_result

    response = client.patch(
        "/auth/me",
        json={"business_name": "Hijacked Name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_update_me_non_owner_without_comment_settings_permission_returns_403(
    client, mock_db, mock_settings, make_test_user
):
    """PATCH /auth/me 403s a non-owner lacking comment_settings even for ig_comment_* fields."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "staff@biz.com"})
    existing = Client(id=1, email="owner@biz.com", hashed_password="x", is_active=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(
        existing, role="staff", permissions=["order_view"], user_id=2
    )
    mock_db.execute.return_value = mock_result

    response = client.patch(
        "/auth/me",
        json={"ig_comment_reply_all": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_update_me_non_owner_with_comment_settings_permission_allowed(
    client, mock_db, mock_settings, make_test_user
):
    """PATCH /auth/me lets a manager holding comment_settings update ig_comment_* fields."""
    from app.models.client import Client

    token = auth_service.create_access_token({"sub": "manager@biz.com"})
    existing = Client(
        id=1, email="owner@biz.com", hashed_password="x", business_name="Biz",
        gemini_system_prompt="be helpful", is_active=True, hsn_code="5007",
        briefing_enabled=True, briefing_time="09:00", dashboard_language="en",
        catalogue_theme_color="#6366F1", accepts_cod=False, accepts_upi=True,
        accepts_bank_transfer=False, onboarding_step=0, onboarding_completed=False,
        plan_slug="starter", daily_message_limit=100,
        ig_comment_autoreply_enabled=False, ig_comment_reply_all=False,
        ig_comment_triggers=["price"], ig_comment_reply_text={"english": "hi"},
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(
        existing, role="manager", permissions=["comment_settings"], user_id=2
    )
    mock_db.execute.return_value = mock_result

    response = client.patch(
        "/auth/me",
        json={"ig_comment_reply_all": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["ig_comment_reply_all"] is True


# --- helpers ---

async def _set_id(obj):
    """
    Side effect to simulate db.refresh() on a newly inserted model.

    A real DB flush applies every column's server-side/Python default; this
    mock never flushes, so non-nullable fields would otherwise stay None and
    fail ClientOut's pydantic validation. Mirror app.models.client.Client's
    declared defaults here. register() also refreshes the owner User row —
    that one only needs an id, so it's handled separately.
    """
    from app.models.user import User

    obj.id = 1
    if isinstance(obj, User):
        return

    obj.business_name = obj.business_name or ""
    from app.models.client import DEFAULT_SYSTEM_PROMPT

    obj.gemini_system_prompt = DEFAULT_SYSTEM_PROMPT
    obj.is_active = True
    obj.hsn_code = "5007"
    obj.briefing_enabled = True
    obj.briefing_time = "09:00"
    obj.dashboard_language = "en"
    obj.catalogue_theme_color = "#6366F1"
    obj.accepts_cod = False
    obj.accepts_upi = True
    obj.accepts_bank_transfer = False
    obj.onboarding_step = 0
    obj.onboarding_completed = False
    obj.plan_slug = obj.plan_slug or "starter"
    obj.daily_message_limit = obj.daily_message_limit or 100
    obj.ig_comment_autoreply_enabled = False
    obj.ig_comment_reply_all = False
    from app.models.client import _default_ig_comment_reply_text, _default_ig_comment_triggers

    obj.ig_comment_triggers = _default_ig_comment_triggers()
    obj.ig_comment_reply_text = _default_ig_comment_reply_text()
