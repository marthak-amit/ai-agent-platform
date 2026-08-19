"""Tests for app/schemas/instagram.py — postback (carousel tap) parsing."""

from app.schemas.instagram import InstagramWebhookPayload


def _postback_payload(sku: str = "PR17761", title: str = "Select") -> dict:
    return {
        "object": "instagram",
        "entry": [{
            "id": "IGID1",
            "messaging": [{
                "sender": {"id": "S1"},
                "recipient": {"id": "R1"},
                "timestamp": 123,
                "postback": {"payload": sku, "title": title},
            }],
        }],
    }


def test_postback_event_parses_without_a_message_field():
    """A tapped carousel button arrives as a postback sibling to (not inside) message."""
    payload = InstagramWebhookPayload.model_validate(_postback_payload())
    dm = payload.get_first_dm()

    assert dm is not None
    assert dm.message is None
    assert dm.postback is not None
    assert dm.get_postback_payload() == "PR17761"


def test_get_postback_payload_returns_none_for_a_normal_text_message():
    """A plain text DM (no postback field) must not error — payload is None."""
    payload = InstagramWebhookPayload.model_validate({
        "object": "instagram",
        "entry": [{
            "id": "IGID1",
            "messaging": [{
                "sender": {"id": "S1"}, "recipient": {"id": "R1"}, "timestamp": 123,
                "message": {"mid": "mid.1", "text": "hello", "type": "text"},
            }],
        }],
    })
    dm = payload.get_first_dm()

    assert dm.postback is None
    assert dm.get_postback_payload() is None
