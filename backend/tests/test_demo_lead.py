"""Tests for app/routers/leads.py's public POST /leads/public endpoint."""

from unittest.mock import AsyncMock

from app.routers import leads as leads_router


def _reset_rate_limit_store():
    """Clear the in-process rate limiter between tests so they don't interfere."""
    leads_router._demo_lead_store.clear()


def test_submit_public_demo_lead_success(client, mock_db):
    """POST /leads/public creates a DemoLead and returns {ok: true} with no leaked IDs."""
    _reset_rate_limit_store()

    response = client.post(
        "/leads/public",
        json={
            "business_name": "Meera Fashions",
            "email": "owner@meerafashions.com",
            "phone": "+919876543210",
            "whatsapp_number": "+919876543210",
            "monthly_order_volume": "50-100 orders/month",
            "message": "Interested in the Growth plan.",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {"ok": True}
    assert mock_db.add.called
    assert mock_db.commit.await_count == 1


def test_submit_public_demo_lead_missing_required_field(client, mock_db):
    """POST /leads/public returns 422 when email is missing."""
    _reset_rate_limit_store()

    response = client.post(
        "/leads/public",
        json={"business_name": "Meera Fashions"},
    )
    assert response.status_code == 422


def test_submit_public_demo_lead_invalid_email(client, mock_db):
    """POST /leads/public returns 422 for a malformed email."""
    _reset_rate_limit_store()

    response = client.post(
        "/leads/public",
        json={"business_name": "Meera Fashions", "email": "not-an-email"},
    )
    assert response.status_code == 422


def test_submit_public_demo_lead_honeypot_silently_ignored(client, mock_db):
    """A filled honeypot field returns 200 ok but never touches the DB."""
    _reset_rate_limit_store()

    response = client.post(
        "/leads/public",
        json={
            "business_name": "Bot Inc",
            "email": "bot@example.com",
            "company_website": "https://spam.example.com",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert not mock_db.add.called
    assert mock_db.commit.await_count == 0


def test_submit_public_demo_lead_rate_limited(client, mock_db):
    """The 6th demo-lead submission from the same client IP within the window is rejected."""
    _reset_rate_limit_store()

    payload = {"business_name": "Meera Fashions", "email": "owner@meerafashions.com"}
    for _ in range(5):
        response = client.post("/leads/public", json=payload)
        assert response.status_code == 200

    response = client.post("/leads/public", json=payload)
    assert response.status_code == 429


def test_submit_public_demo_lead_skips_notify_when_unset(client, mock_db, monkeypatch):
    """No WhatsApp send is attempted when INTERNAL_LEAD_NOTIFY_NUMBER is unset."""
    _reset_rate_limit_store()
    monkeypatch.setattr(
        leads_router, "get_settings", lambda: _FakeSettings(internal_lead_notify_number="")
    )
    send_mock = AsyncMock()
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", send_mock)

    response = client.post(
        "/leads/public",
        json={"business_name": "Meera Fashions", "email": "owner@meerafashions.com"},
    )
    assert response.status_code == 200
    send_mock.assert_not_called()


def test_submit_public_demo_lead_sends_notify_when_configured(client, mock_db, monkeypatch):
    """A configured INTERNAL_LEAD_NOTIFY_NUMBER triggers a WhatsApp send with the right content."""
    _reset_rate_limit_store()
    monkeypatch.setattr(
        leads_router,
        "get_settings",
        lambda: _FakeSettings(internal_lead_notify_number="919999999999"),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", send_mock)

    response = client.post(
        "/leads/public",
        json={
            "business_name": "Meera Fashions",
            "email": "owner@meerafashions.com",
            "whatsapp_number": "+919876543210",
            "monthly_order_volume": "50-100/mo",
            "message": "Need this ASAP",
        },
    )
    assert response.status_code == 200
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["to_phone_number"] == "919999999999"
    assert "Meera Fashions" in kwargs["message_text"]
    assert "owner@meerafashions.com" in kwargs["message_text"]
    assert "+919876543210" in kwargs["message_text"]
    assert "50-100/mo" in kwargs["message_text"]
    assert "Need this ASAP" in kwargs["message_text"]


def test_submit_public_demo_lead_notify_failure_does_not_fail_request(client, mock_db, monkeypatch):
    """A WhatsApp send failure is logged but does not change the 200 response."""
    _reset_rate_limit_store()
    monkeypatch.setattr(
        leads_router,
        "get_settings",
        lambda: _FakeSettings(internal_lead_notify_number="919999999999"),
    )
    send_mock = AsyncMock(side_effect=RuntimeError("Meta API down"))
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", send_mock)

    response = client.post(
        "/leads/public",
        json={"business_name": "Meera Fashions", "email": "owner@meerafashions.com"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    send_mock.assert_awaited_once()


def test_submit_public_demo_lead_honeypot_never_notifies(client, mock_db, monkeypatch):
    """A honeypot-triggered submission never sends an internal WhatsApp notification."""
    _reset_rate_limit_store()
    monkeypatch.setattr(
        leads_router,
        "get_settings",
        lambda: _FakeSettings(internal_lead_notify_number="919999999999"),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", send_mock)

    response = client.post(
        "/leads/public",
        json={
            "business_name": "Bot Inc",
            "email": "bot@example.com",
            "company_website": "https://spam.example.com",
        },
    )
    assert response.status_code == 200
    send_mock.assert_not_called()


class _FakeSettings:
    """Minimal stand-in for app.config.Settings — only the field leads.py reads."""

    def __init__(self, internal_lead_notify_number: str) -> None:
        self.internal_lead_notify_number = internal_lead_notify_number
