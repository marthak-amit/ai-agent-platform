"""
Tests for app/routers/whatsapp_signup.py — WhatsApp Embedded Signup flow.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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


def test_signup_config_disabled_without_config_id(client, mock_db, mock_settings, make_test_user):
    """GET /integrations/whatsapp/signup-config reports enabled=False with no META_WHATSAPP_CONFIG_ID."""
    existing = _make_client()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.get(
        "/integrations/whatsapp/signup-config", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False


def test_signup_config_enabled_when_configured(client, mock_db, mock_settings, make_test_user):
    """GET /integrations/whatsapp/signup-config returns app_id/config_id once configured."""
    existing = _make_client()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    mock_settings.meta_app_id = "123456"
    mock_settings.meta_whatsapp_config_id = "789"

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.get(
        "/integrations/whatsapp/signup-config", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {"enabled": True, "app_id": "123456", "config_id": "789"}


def test_embedded_signup_rejected_when_not_configured(client, mock_db, mock_settings, make_test_user):
    """POST /integrations/whatsapp/embedded-signup 400s when the server has no config_id set."""
    existing = _make_client()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    response = client.post(
        "/integrations/whatsapp/embedded-signup",
        json={"code": "abc", "waba_id": "1", "phone_number_id": "2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


def test_embedded_signup_stores_token_on_caller_client(client, mock_db, mock_settings, make_test_user):
    """
    A successful exchange stores the long-lived token, phone_number_id, and
    display number only on the JWT-authenticated caller's own Client row.
    """
    existing = _make_client(client_id=7)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    mock_settings.meta_app_id = "123456"
    mock_settings.meta_whatsapp_config_id = "789"

    dummy_request = httpx.Request("GET", "https://graph.facebook.com/")
    fake_responses = [
        httpx.Response(200, json={"access_token": "short-lived-token"}, request=dummy_request),
        httpx.Response(200, json={"access_token": "long-lived-token"}, request=dummy_request),
        httpx.Response(200, json={"success": True}, request=dummy_request),  # subscribed_apps
        httpx.Response(200, json={"display_phone_number": "+91 98765 43210"}, request=dummy_request),
    ]

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=[fake_responses[0], fake_responses[1], fake_responses[3]])), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=fake_responses[2])):
        response = client.post(
            "/integrations/whatsapp/embedded-signup",
            json={"code": "auth-code-123", "waba_id": "999", "phone_number_id": "555"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["whatsapp_phone_number_id"] == "555"
    assert existing.whatsapp_access_token == "long-lived-token"
    assert existing.whatsapp_phone_number_id == "555"
    assert existing.whatsapp_number == "+91 98765 43210"
    mock_db.commit.assert_awaited()


def test_embedded_signup_502s_when_subscribe_fails(client, mock_db, mock_settings, make_test_user):
    """A failed subscribed_apps call surfaces as 502 and never writes credentials."""
    existing = _make_client(client_id=7)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = make_test_user(existing)
    mock_db.execute.return_value = mock_result

    mock_settings.meta_app_id = "123456"
    mock_settings.meta_whatsapp_config_id = "789"

    dummy_request = httpx.Request("GET", "https://graph.facebook.com/")
    fake_get_responses = [
        httpx.Response(200, json={"access_token": "short-lived-token"}, request=dummy_request),
        httpx.Response(200, json={"access_token": "long-lived-token"}, request=dummy_request),
    ]
    fake_subscribe_response = httpx.Response(400, json={"error": "bad"}, request=dummy_request)

    token = auth_service.create_access_token({"sub": "owner@biz.com"})
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=fake_get_responses)), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=fake_subscribe_response)):
        response = client.post(
            "/integrations/whatsapp/embedded-signup",
            json={"code": "auth-code-123", "waba_id": "999", "phone_number_id": "555"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 502
    assert existing.whatsapp_access_token is None
