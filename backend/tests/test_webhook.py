"""
Tests for app/routers/webhook.py.

Covers GET verification, _verify_signature helper, and POST message handling.
conversation_service, lead_service, gemini_service, and whatsapp_service are
all mocked so no DB or external API calls are made.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.webhook import _verify_signature

VALID_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "123",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550000000",
                            "phone_number_id": "1234567890",
                        },
                        "contacts": [
                            {"profile": {"name": "Test User"}, "wa_id": "919999999999"}
                        ],
                        "messages": [
                            {
                                "from": "919999999999",
                                "id": "wamid.test",
                                "timestamp": "1716800000",
                                "type": "text",
                                "text": {"body": "Hello"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


def _make_signature(body: bytes, secret: str = "test-app-secret") -> str:
    """Compute a valid X-Hub-Signature-256 header value."""
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# --- GET /webhook ---


def test_verify_webhook_success(client):
    """GET with correct token returns hub.challenge."""
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge_abc",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge_abc"


def test_verify_webhook_wrong_token(client):
    """GET with wrong token returns 403."""
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge_abc",
        },
    )
    assert response.status_code == 403


def test_verify_webhook_wrong_mode(client):
    """GET with mode != subscribe returns 403."""
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge_abc",
        },
    )
    assert response.status_code == 403


# --- _verify_signature ---


def test_verify_signature_valid(mock_settings):
    """Valid HMAC returns True."""
    body = b'{"object":"test"}'
    assert _verify_signature(body, _make_signature(body)) is True


def test_verify_signature_invalid(mock_settings):
    """Tampered body returns False."""
    body = b'{"object":"test"}'
    assert _verify_signature(body, _make_signature(b'{"object":"other"}')) is False


def test_verify_signature_missing_prefix(mock_settings):
    """Header without sha256= prefix returns False."""
    assert _verify_signature(b"body", "abcdef1234") is False


# --- POST /webhook ---


@patch("app.routers.webhook.conversation_service.get_or_create_conversation", new_callable=AsyncMock)
@patch("app.routers.webhook.conversation_service.get_history", new_callable=AsyncMock)
@patch("app.routers.webhook.conversation_service.save_message", new_callable=AsyncMock)
@patch("app.routers.webhook.lead_service.tag_lead", new_callable=AsyncMock)
@patch("app.routers.webhook.gemini_service.generate_reply", new_callable=AsyncMock)
@patch("app.routers.webhook.whatsapp_service.send_text_message", new_callable=AsyncMock)
@patch("app.routers.webhook._get_system_prompt", return_value=None)
@patch("app.routers.webhook._get_catalogue_context", new_callable=AsyncMock)
@patch("app.routers.webhook._record_usage", new_callable=AsyncMock)
def test_receive_message_success(
    mock_usage, mock_catalogue, mock_prompt, mock_send, mock_gemini, mock_lead, mock_save, mock_history, mock_conv, client
):
    """Valid signed payload triggers full pipeline and returns 200."""
    mock_conv.return_value = MagicMock(id=1)
    mock_history.return_value = []
    mock_prompt.return_value = None
    mock_catalogue.return_value = None
    mock_gemini.return_value = "AI reply"
    mock_send.return_value = {"messages": [{"id": "wamid.reply"}]}

    body = json.dumps(VALID_PAYLOAD).encode()
    sig = _make_signature(body)

    response = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_gemini.assert_called_once()
    call_args, call_kwargs = mock_gemini.call_args
    assert call_args[0] == "Hello"
    assert call_kwargs["history"] == []
    assert call_kwargs["catalogue_context"] is None
    assert isinstance(call_kwargs["system_prompt"], str)
    mock_send.assert_called_once_with(to_phone_number="919999999999", message_text="AI reply")


def test_receive_message_invalid_signature(client):
    """POST with bad signature returns 401."""
    body = json.dumps(VALID_PAYLOAD).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=badsig"},
    )
    assert response.status_code == 401


def test_receive_status_update(client):
    """Status update (no messages) returns 200 without calling Gemini."""
    status_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "1234567890",
                            },
                            "statuses": [{"id": "x", "status": "delivered", "timestamp": "123"}],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    body = json.dumps(status_payload).encode()
    sig = _make_signature(body)

    with patch("app.routers.webhook.gemini_service.generate_reply") as mock_gemini:
        response = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert response.status_code == 200
        mock_gemini.assert_not_called()


def test_receive_non_text_message(client):
    """Unsupported message type (e.g. video) is skipped and returns 200."""
    video_payload = json.loads(json.dumps(VALID_PAYLOAD))
    msg = video_payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg["type"] = "video"
    msg.pop("text", None)

    body = json.dumps(video_payload).encode()
    sig = _make_signature(body)

    with patch("app.routers.webhook.gemini_service.generate_reply") as mock_gemini:
        response = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert response.status_code == 200
        mock_gemini.assert_not_called()
