"""
Tests for app/services/instagram_service.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture
def mock_httpx_client():
    mock_response = MagicMock()
    mock_response.json.return_value = {"recipient_id": "USER_ID", "message_id": "mid.123"}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client, mock_response


async def test_send_dm_posts_to_correct_url(mock_httpx_client, mock_settings):
    """send_dm posts to /{ig_user_id}/messages with Bearer token."""
    from app.services.instagram_service import _raw_send_dm as send_dm

    mock_client, _ = mock_httpx_client
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        result = await send_dm("IG_USER_123", "USER_IGSID_456", "Hello from AI")

    assert result == {"recipient_id": "USER_ID", "message_id": "mid.123"}
    url = mock_client.post.call_args[0][0]
    assert "IG_USER_123" in url
    assert "messages" in url
    headers = mock_client.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer test-ig-token"


async def test_reply_to_comment_posts_to_correct_url(mock_httpx_client, mock_settings):
    """reply_to_comment posts to /{comment_id}/replies."""
    from app.services.instagram_service import _raw_reply_to_comment as reply_to_comment

    mock_client, _ = mock_httpx_client
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        await reply_to_comment("IG_USER_123", "COMMENT_ID_789", "Thank you!")

    url = mock_client.post.call_args[0][0]
    assert "COMMENT_ID_789" in url
    assert "replies" in url


async def test_send_private_reply_posts_to_messages_with_comment_id_recipient(mock_httpx_client, mock_settings):
    """send_private_reply posts to /{ig_user_id}/messages with recipient.comment_id."""
    from app.services.instagram_service import _raw_send_private_reply as send_private_reply

    mock_client, _ = mock_httpx_client
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        await send_private_reply("IG_USER_123", "COMMENT_ID_789", "Check your DM 👀")

    url = mock_client.post.call_args[0][0]
    assert "IG_USER_123" in url
    assert "messages" in url
    payload = mock_client.post.call_args[1]["json"]
    assert payload["recipient"] == {"comment_id": "COMMENT_ID_789"}
    assert payload["message"] == {"text": "Check your DM 👀"}


async def test_send_generic_template_posts_carousel_payload(mock_settings):
    """send_generic_template posts a template/generic attachment with the given elements."""
    from app.services.instagram_service import _raw_send_generic_template

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    elements = [
        {
            "title": "Traditional Choli", "subtitle": "₹3,999",
            "image_url": "https://media.example.com/1/1/a.jpg",
            "buttons": [{"type": "postback", "title": "Select", "payload": "PR17761"}],
        },
        {
            "title": "Designer Lehenga", "subtitle": "₹6,500",
            "buttons": [{"type": "postback", "title": "Select", "payload": "LH10042"}],
        },
    ]
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        result = await _raw_send_generic_template("IG_USER_123", "USER_IGSID_456", elements)

    assert result is True
    url = mock_client.post.call_args[0][0]
    assert "IG_USER_123" in url
    payload = mock_client.post.call_args[1]["json"]
    assert payload["recipient"] == {"id": "USER_IGSID_456"}
    attachment = payload["message"]["attachment"]
    assert attachment["type"] == "template"
    assert attachment["payload"]["template_type"] == "generic"
    assert attachment["payload"]["elements"] == elements


async def test_send_generic_template_caps_at_ten_elements(mock_settings):
    """More than 10 elements are truncated before the request is sent (Meta's carousel limit)."""
    from app.services.instagram_service import _raw_send_generic_template

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    elements = [{"title": f"Product {i}", "subtitle": "₹100"} for i in range(15)]
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        await _raw_send_generic_template("IG_USER_123", "USER_IGSID_456", elements)

    payload = mock_client.post.call_args[1]["json"]
    assert len(payload["message"]["attachment"]["payload"]["elements"]) == 10


async def test_send_generic_template_returns_false_on_http_error(mock_settings):
    """send_generic_template returns False (not raise) on a non-200 response."""
    from app.services.instagram_service import _raw_send_generic_template

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        result = await _raw_send_generic_template("IG_USER_123", "USER_IGSID_456", [])

    assert result is False


async def test_send_dm_raises_on_http_error(mock_settings):
    """send_dm propagates HTTPStatusError on 4xx/5xx."""
    from app.services.instagram_service import _raw_send_dm as send_dm

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400", request=MagicMock(), response=MagicMock()
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await send_dm("IG_USER", "IGSID", "Test")
