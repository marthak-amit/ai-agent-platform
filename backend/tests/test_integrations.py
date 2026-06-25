"""
Tests for app/routers/integrations.py — Instagram OAuth connect flow.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import jwt

from app.routers.integrations import _create_state_token, _verify_state_token
from app.services import auth_service


def _make_client(client_id=1, email="owner@biz.com", **overrides):
    from app.models.client import Client, DEFAULT_SYSTEM_PROMPT

    defaults = dict(
        id=client_id,
        email=email,
        hashed_password="x",
        business_name="My Biz",
        gemini_system_prompt=DEFAULT_SYSTEM_PROMPT,
        is_active=True,
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
    )
    defaults.update(overrides)
    return Client(**defaults)


def test_state_token_round_trips_client_id(mock_settings):
    """A freshly minted state token decodes back to the same client_id."""
    state = _create_state_token(42)
    assert _verify_state_token(state) == 42


def test_state_token_rejects_garbage(mock_settings):
    """An unsigned/garbage state value is rejected, not silently accepted."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _verify_state_token("not-a-real-token")


def test_state_token_rejects_wrong_purpose(mock_settings):
    """A token signed with the same secret but a different purpose is rejected."""
    from fastapi import HTTPException

    bogus = jwt.encode(
        {"client_id": 1, "purpose": "something_else"},
        "test-secret-key",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        _verify_state_token(bogus)


def test_connect_returns_authorize_url_bound_to_caller(client, mock_db, mock_settings):
    """GET /integrations/instagram/connect embeds a state token for the calling client."""
    existing = _make_client(client_id=7, email="owner@biz.com")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.get(
        "/integrations/instagram/connect", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    url = response.json()["url"]
    assert "state=" in url

    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(url).query)["state"][0]
    assert _verify_state_token(state) == 7


def test_callback_writes_only_the_bound_clients_row(client, mock_db, mock_settings):
    """
    The OAuth callback must write IG credentials only onto the client_id bound
    in the state token — never onto whichever row mock_db.execute happens to
    return next (simulating "some other client's row" being queried elsewhere
    in the same request).
    """
    target_client = _make_client(client_id=7, email="owner@biz.com")
    state = _create_state_token(7)

    mock_db.get = AsyncMock(return_value=target_client)

    dummy_request = httpx.Request("GET", "https://graph.facebook.com/")
    fake_responses = [
        httpx.Response(200, json={"access_token": "short-lived-token"}, request=dummy_request),
        httpx.Response(200, json={"access_token": "long-lived-token"}, request=dummy_request),
        httpx.Response(
            200,
            json={"data": [{"instagram_business_account": {"id": "1784145800001"}}]},
            request=dummy_request,
        ),
    ]

    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=fake_responses)):
        response = client.get(
            "/integrations/instagram/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )

    assert response.status_code in (302, 307)
    assert "ig_status=connected" in response.headers["location"]
    mock_db.get.assert_awaited_once_with(target_client.__class__, 7)
    assert target_client.instagram_access_token == "long-lived-token"
    assert target_client.instagram_account_id == "1784145800001"
    mock_db.commit.assert_awaited()


def test_callback_rejects_replayed_or_mismatched_state(client, mock_db, mock_settings):
    """A tampered/replayed state value is rejected with a redirect to an error status, not a write."""
    mock_db.get = AsyncMock()

    bogus_state = jwt.encode(
        {"client_id": 999, "purpose": "ig_oauth_state"},
        "wrong-secret",
        algorithm="HS256",
    )
    response = client.get(
        "/integrations/instagram/callback",
        params={"code": "auth-code-123", "state": bogus_state},
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "ig_status=error" in response.headers["location"]
    mock_db.get.assert_not_awaited()


def test_callback_handles_user_cancellation(client, mock_db, mock_settings):
    """If the user cancels Meta's consent screen, redirect with a cancelled status, not a 500."""
    response = client.get(
        "/integrations/instagram/callback",
        params={"error": "access_denied", "error_description": "User denied"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "ig_status=cancelled" in response.headers["location"]


def test_disconnect_clears_credentials(client, mock_db, mock_settings):
    """POST /integrations/instagram/disconnect nulls out both IG columns for the caller."""
    existing = _make_client(
        client_id=7,
        email="owner@biz.com",
        instagram_access_token="some-token",
        instagram_account_id="178414581234567",
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    mock_db.execute.return_value = mock_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.post(
        "/integrations/instagram/disconnect", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert existing.instagram_access_token is None
    assert existing.instagram_account_id is None
