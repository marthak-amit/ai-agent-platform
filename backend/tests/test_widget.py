"""
Tests for app/routers/widget.py.

Covers the two public endpoints:
  POST /widget/message  — AI reply pipeline with api_key auth and plan guard
  GET  /widget/config/{api_key} — branding config

Mocking strategy:
  - Message tests: mock _get_client_by_api_key at the router level (avoids
    the AsyncMock.scalar_one_or_none coroutine issue in Python 3.14).
  - Config tests: set mock_db.execute.return_value to an explicit MagicMock
    so scalar_one_or_none() returns a value, not a coroutine.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client(
    plan_slug: str = "pro",
    api_key: str = "vp_testkey123",
    business_name: str = "Test Shop",
    system_prompt: str = "You are a helpful assistant.",
    client_id: int = 1,
) -> MagicMock:
    """Build a mock Client ORM instance with configurable fields."""
    c = MagicMock()
    c.id = client_id
    c.api_key = api_key
    c.plan_slug = plan_slug
    c.business_name = business_name
    c.gemini_system_prompt = system_prompt
    c.is_active = True
    return c


def _db_returning(value):
    """
    Return a MagicMock that simulates db.execute(...).scalar_one_or_none()
    without triggering the Python 3.14 AsyncMock-as-coroutine behaviour.
    """
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = value
    return mock_result


VALID_BODY = {
    "api_key": "vp_testkey123",
    "session_id": "session-abc-123",
    "message": "What sarees do you have?",
}

# ── POST /widget/message ──────────────────────────────────────────────────────

def test_widget_message_success(client, mock_db):
    """Valid api_key + pro plan returns the AI reply."""
    mock_client = _make_client()

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=True), \
         patch("app.routers.widget.conversation_service.get_or_create_conversation",
               new=AsyncMock(return_value=MagicMock(id=10))), \
         patch("app.routers.widget.conversation_service.get_history",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.list_products",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.search_products",
               return_value=[]), \
         patch("app.routers.widget.gemini_service.generate_reply",
               new=AsyncMock(return_value="We have Banarasi and Kanjivaram sarees!")), \
         patch("app.routers.widget.conversation_service.save_message",
               new=AsyncMock()), \
         patch("app.routers.widget.usage_service.record_message",
               new=AsyncMock()), \
         patch("app.routers.widget.lead_service.tag_lead",
               new=AsyncMock()):
        response = client.post("/widget/message", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "We have Banarasi and Kanjivaram sarees!"
    assert body["session_id"] == "session-abc-123"


def test_widget_message_invalid_api_key(client, mock_db):
    """Unknown api_key returns 401."""
    from fastapi import HTTPException
    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(side_effect=HTTPException(status_code=401,
                                                        detail="Invalid or inactive API key."))):
        response = client.post("/widget/message", json=VALID_BODY)

    assert response.status_code == 401


def test_widget_message_plan_restricted(client, mock_db):
    """Starter-plan client gets 403 — website widget requires pro."""
    mock_client = _make_client(plan_slug="starter")

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=False):
        response = client.post("/widget/message", json=VALID_BODY)

    assert response.status_code == 403
    assert "Pro" in response.json()["detail"]


def test_widget_message_growth_plan_restricted(client, mock_db):
    """Growth-plan client also gets 403 — website widget is pro-only."""
    mock_client = _make_client(plan_slug="growth")

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=False):
        response = client.post("/widget/message", json=VALID_BODY)

    assert response.status_code == 403


def test_widget_message_gemini_error_returns_503(client, mock_db):
    """When Gemini raises the endpoint returns 503, not an unhandled 500."""
    mock_client = _make_client()

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=True), \
         patch("app.routers.widget.conversation_service.get_or_create_conversation",
               new=AsyncMock(return_value=MagicMock(id=10))), \
         patch("app.routers.widget.conversation_service.get_history",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.list_products",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.search_products",
               return_value=[]), \
         patch("app.routers.widget.gemini_service.generate_reply",
               new=AsyncMock(side_effect=RuntimeError("Gemini down"))):
        response = client.post("/widget/message", json=VALID_BODY)

    assert response.status_code == 503


def test_widget_message_calls_gemini_with_system_prompt(client, mock_db):
    """Gemini is called with the client's configured system prompt."""
    mock_client = _make_client(system_prompt="You are a saree expert.")
    gemini_mock = AsyncMock(return_value="Great choice!")

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=True), \
         patch("app.routers.widget.conversation_service.get_or_create_conversation",
               new=AsyncMock(return_value=MagicMock(id=10))), \
         patch("app.routers.widget.conversation_service.get_history",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.list_products",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.search_products",
               return_value=[]), \
         patch("app.routers.widget.gemini_service.generate_reply",
               new=gemini_mock), \
         patch("app.routers.widget.conversation_service.save_message",
               new=AsyncMock()), \
         patch("app.routers.widget.usage_service.record_message",
               new=AsyncMock()), \
         patch("app.routers.widget.lead_service.tag_lead",
               new=AsyncMock()):
        client.post("/widget/message", json=VALID_BODY)

    call_kwargs = gemini_mock.call_args.kwargs
    assert call_kwargs["system_prompt"] == "You are a saree expert."


def test_widget_message_passes_conversation_history(client, mock_db):
    """Existing conversation history is passed to Gemini."""
    mock_client = _make_client()
    prior_msg = MagicMock()
    prior_msg.role = "user"
    prior_msg.content = "earlier message"
    gemini_mock = AsyncMock(return_value="Follow-up reply")

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=True), \
         patch("app.routers.widget.conversation_service.get_or_create_conversation",
               new=AsyncMock(return_value=MagicMock(id=10))), \
         patch("app.routers.widget.conversation_service.get_history",
               new=AsyncMock(return_value=[prior_msg])), \
         patch("app.routers.widget.catalogue_service.list_products",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.search_products",
               return_value=[]), \
         patch("app.routers.widget.gemini_service.generate_reply",
               new=gemini_mock), \
         patch("app.routers.widget.conversation_service.save_message",
               new=AsyncMock()), \
         patch("app.routers.widget.usage_service.record_message",
               new=AsyncMock()), \
         patch("app.routers.widget.lead_service.tag_lead",
               new=AsyncMock()):
        client.post("/widget/message", json=VALID_BODY)

    call_kwargs = gemini_mock.call_args.kwargs
    assert call_kwargs["history"] == [{"role": "user", "content": "earlier message"}]


def test_widget_message_saves_both_messages(client, mock_db):
    """Both the user message and AI reply are persisted to the DB."""
    mock_client = _make_client()
    save_mock = AsyncMock()

    with patch("app.routers.widget._get_client_by_api_key",
               new=AsyncMock(return_value=mock_client)), \
         patch("app.routers.widget.plan_service.plan_allows_channel",
               return_value=True), \
         patch("app.routers.widget.conversation_service.get_or_create_conversation",
               new=AsyncMock(return_value=MagicMock(id=10))), \
         patch("app.routers.widget.conversation_service.get_history",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.list_products",
               new=AsyncMock(return_value=[])), \
         patch("app.routers.widget.catalogue_service.search_products",
               return_value=[]), \
         patch("app.routers.widget.gemini_service.generate_reply",
               new=AsyncMock(return_value="AI says hello")), \
         patch("app.routers.widget.conversation_service.save_message",
               new=save_mock), \
         patch("app.routers.widget.usage_service.record_message",
               new=AsyncMock()), \
         patch("app.routers.widget.lead_service.tag_lead",
               new=AsyncMock()):
        client.post("/widget/message", json=VALID_BODY)

    assert save_mock.call_count == 2
    roles_saved = [call.args[2] for call in save_mock.call_args_list]
    assert "user" in roles_saved
    assert "model" in roles_saved


# ── GET /widget/config/{api_key} ──────────────────────────────────────────────

def test_widget_config_success(client, mock_db):
    """Valid api_key returns business name, welcome message, and brand color."""
    mock_client = _make_client(business_name="Riya Sarees")
    # Use explicit MagicMock so scalar_one_or_none() is sync, not a coroutine.
    mock_db.execute.return_value = _db_returning(mock_client)

    response = client.get("/widget/config/vp_testkey123")

    assert response.status_code == 200
    body = response.json()
    assert body["business_name"] == "Riya Sarees"
    assert "Riya Sarees" in body["welcome_message"]
    assert body["brand_color"] == "#6366f1"


def test_widget_config_invalid_key_returns_404(client, mock_db):
    """Unknown api_key returns 404."""
    mock_db.execute.return_value = _db_returning(None)

    response = client.get("/widget/config/vp_nonexistent")

    assert response.status_code == 404


def test_widget_config_fallback_business_name(client, mock_db):
    """Client with empty business_name falls back to 'AI Assistant'."""
    mock_client = _make_client(business_name="")
    mock_db.execute.return_value = _db_returning(mock_client)

    response = client.get("/widget/config/vp_testkey123")

    assert response.status_code == 200
    assert response.json()["business_name"] == "AI Assistant"


def test_widget_config_welcome_message_contains_business_name(client, mock_db):
    """Welcome message dynamically includes the business name."""
    mock_client = _make_client(business_name="Quick Clinic")
    mock_db.execute.return_value = _db_returning(mock_client)

    response = client.get("/widget/config/vp_testkey123")

    assert "Quick Clinic" in response.json()["welcome_message"]
