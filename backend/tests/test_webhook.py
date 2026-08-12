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
@patch("app.services.order_pipeline.conversation_service.get_history", new_callable=AsyncMock)
@patch("app.services.order_pipeline.conversation_service.save_message", new_callable=AsyncMock)
@patch("app.services.order_pipeline.conversation_service.update_stage", new_callable=AsyncMock)
@patch("app.services.order_pipeline.lead_service.tag_lead", new_callable=AsyncMock)
@patch("app.services.order_pipeline.gemini_service.generate_reply", new_callable=AsyncMock)
@patch("app.services.whatsapp_service._raw_send_text_message", new_callable=AsyncMock)
@patch("app.services.order_pipeline._get_system_prompt", return_value=None)
@patch("app.services.order_pipeline._get_catalogue_context", new_callable=AsyncMock)
@patch("app.services.order_pipeline._record_usage", new_callable=AsyncMock)
@patch("app.routers.webhook._get_client_by_phone_number_id", new_callable=AsyncMock)
def test_receive_message_success(
    mock_get_client, mock_usage, mock_catalogue, mock_prompt, mock_send, mock_gemini, mock_lead, mock_update_stage, mock_save, mock_history, mock_conv, mock_db, client
):
    """Valid signed payload triggers full pipeline and returns 200."""
    mock_get_client.return_value = MagicMock(
        id=1, business_name="Test Store", catalogue_slug=None,
        whatsapp_phone_number_id="1234567890", is_active=True,
        flow_state_ttl_hours=None, context_ttl_days=None,
    )
    mock_conv.return_value = MagicMock(
        id=1, stage="greeting", pending_product_sku=None,
        last_customer_language="english",
        flow_state_at=None, last_context_at=None,
    )
    mock_history.return_value = []
    mock_prompt.return_value = None
    mock_catalogue.return_value = (None, [])  # (catalogue_context, canonical_browse_products)
    mock_gemini.return_value = "AI reply"
    mock_send.return_value = {"messages": [{"id": "wamid.reply"}]}
    # All db.execute calls must return a plain MagicMock (not AsyncMock) so that
    # scalar_one_or_none() / scalars().all() / first() are regular (non-coroutine)
    # callables that return None / [].
    _db_result = MagicMock(
        scalar_one_or_none=MagicMock(return_value=None),
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        first=MagicMock(return_value=None),
    )
    mock_db.execute = AsyncMock(return_value=_db_result)

    body = json.dumps(VALID_PAYLOAD).encode()
    sig = _make_signature(body)

    response = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # "Hello" is a pure greeting — routes to TEMPLATE, no LLM call.
    mock_gemini.assert_not_called()
    # Template reply is sent.
    mock_send.assert_called_once()
    sent_args = mock_send.call_args
    sent_text = sent_args[0][1] if sent_args[0] else sent_args[1].get("message_text", "")
    assert sent_text  # non-empty template reply was sent


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

    with patch("app.services.order_pipeline.gemini_service.generate_reply") as mock_gemini:
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

    with patch("app.services.order_pipeline.gemini_service.generate_reply") as mock_gemini:
        response = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert response.status_code == 200
        mock_gemini.assert_not_called()
