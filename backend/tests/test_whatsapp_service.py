"""
Tests for app/services/whatsapp_service.py.

Mocks httpx.AsyncClient so no real HTTP call is made.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture
def mock_httpx_client():
    """Patch httpx.AsyncClient to return a successful mock response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "wamid.reply123"}]}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    return mock_client, mock_response


async def test_send_text_message_success(mock_httpx_client, mock_settings):
    """send_text_message posts to correct Meta URL and returns parsed JSON."""
    from app.services.whatsapp_service import send_text_message

    mock_client, _ = mock_httpx_client

    with patch("app.services.whatsapp_service.httpx.AsyncClient", return_value=mock_client):
        result = await send_text_message("919999999999", "Hello from AI")

    assert result == {"messages": [{"id": "wamid.reply123"}]}

    call_args = mock_client.post.call_args
    url = call_args[0][0]
    assert "1234567890" in url
    assert "v21.0" in url

    payload = call_args[1]["json"]
    assert payload["to"] == "919999999999"
    assert payload["text"]["body"] == "Hello from AI"
    assert payload["messaging_product"] == "whatsapp"


async def test_send_text_message_uses_bearer_token(mock_httpx_client, mock_settings):
    """send_text_message sets Authorization header with the access token."""
    from app.services.whatsapp_service import send_text_message

    mock_client, _ = mock_httpx_client

    with patch("app.services.whatsapp_service.httpx.AsyncClient", return_value=mock_client):
        await send_text_message("919999999999", "Test")

    headers = mock_client.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer test-wa-token"


async def test_send_text_message_raises_on_http_error(mock_settings):
    """send_text_message propagates HTTPStatusError on 4xx/5xx."""
    from app.services.whatsapp_service import send_text_message

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=MagicMock(), response=MagicMock()
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.whatsapp_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await send_text_message("919999999999", "Test")


async def test_send_image_message_success(mock_httpx_client, mock_settings):
    """send_image_message posts an image payload with link and caption."""
    from app.services.whatsapp_service import send_image_message

    mock_client, _ = mock_httpx_client

    with patch("app.services.whatsapp_service.httpx.AsyncClient", return_value=mock_client):
        result = await send_image_message(
            "919999999999",
            "https://example.com/product.jpg",
            caption="Designer Lehenga — ₹6,500",
        )

    assert result == {"messages": [{"id": "wamid.reply123"}]}

    payload = mock_client.post.call_args[1]["json"]
    assert payload["type"] == "image"
    assert payload["image"]["link"] == "https://example.com/product.jpg"
    assert payload["image"]["caption"] == "Designer Lehenga — ₹6,500"
    assert payload["to"] == "919999999999"
