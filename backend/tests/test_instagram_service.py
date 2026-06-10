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
    from app.services.instagram_service import send_dm

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
    from app.services.instagram_service import reply_to_comment

    mock_client, _ = mock_httpx_client
    with patch("app.services.instagram_service.httpx.AsyncClient", return_value=mock_client):
        await reply_to_comment("IG_USER_123", "COMMENT_ID_789", "Thank you!")

    url = mock_client.post.call_args[0][0]
    assert "COMMENT_ID_789" in url
    assert "replies" in url


async def test_send_dm_raises_on_http_error(mock_settings):
    """send_dm propagates HTTPStatusError on 4xx/5xx."""
    from app.services.instagram_service import send_dm

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
