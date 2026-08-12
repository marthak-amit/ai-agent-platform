"""
WhatsApp webhook payload builders and HMAC signer for the replay harness.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from tests.replay.conftest import META_APP_SECRET, WA_PHONE_NUMBER_ID


def _sign(payload_bytes: bytes) -> str:
    """Return X-Hub-Signature-256 header value for *payload_bytes*."""
    sig = hmac.new(
        key=META_APP_SECRET.encode(),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


def wa_text_payload(
    sender: str,
    text: str,
    *,
    wamid: str | None = None,
    phone_number_id: str = WA_PHONE_NUMBER_ID,
    ts: int | None = None,
) -> tuple[bytes, dict]:
    """
    Build a minimal WhatsApp text-message webhook payload.

    Returns (body_bytes, headers) ready to POST to /webhook.
    """
    wamid = wamid or f"wamid.test.{sender}.{int(time.time() * 1000)}"
    ts = ts or int(time.time())
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "0",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wamid,
                                    "timestamp": str(ts),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}


async def send_message(
    client,
    sender: str,
    text: str,
    *,
    wamid: str | None = None,
    phone_number_id: str = WA_PHONE_NUMBER_ID,
):
    """POST a WhatsApp text message through the replay HTTP client and return the response."""
    body, headers = wa_text_payload(sender, text, wamid=wamid, phone_number_id=phone_number_id)
    resp = await client.post("/webhook", content=body, headers=headers)
    return resp


def wa_button_payload(
    sender: str,
    button_id: str,
    button_title: str,
    *,
    wamid: str | None = None,
    phone_number_id: str = WA_PHONE_NUMBER_ID,
    ts: int | None = None,
) -> tuple[bytes, dict]:
    """
    Build a WhatsApp interactive button_reply webhook payload.

    Returns (body_bytes, headers) ready to POST to /webhook.
    """
    wamid = wamid or f"wamid.btn.{sender}.{int(time.time() * 1000)}"
    ts = ts or int(time.time())
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "0",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wamid,
                                    "timestamp": str(ts),
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": button_id,
                                            "title": button_title,
                                        },
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}


async def send_button(
    client,
    sender: str,
    button_id: str,
    button_title: str,
    *,
    wamid: str | None = None,
    phone_number_id: str = WA_PHONE_NUMBER_ID,
):
    """POST a WhatsApp interactive button_reply through the replay HTTP client."""
    body, headers = wa_button_payload(
        sender, button_id, button_title,
        wamid=wamid, phone_number_id=phone_number_id,
    )
    resp = await client.post("/webhook", content=body, headers=headers)
    return resp


def capture_all(monkeypatch) -> list[str]:
    """
    Patch send_text_message, send_button_message, and send_list_message so
    that the body text of every outbound reply (plain text, button prompt, or
    list prompt) lands in one shared, order-preserving list.

    Returns the list the test should assert against — equivalent to
    `captured` in tests that previously patched only send_text_message.
    """
    captured: list[str] = []

    async def _capture_text(to_phone_number, message_text):
        captured.append(message_text)

    async def _capture_button(to_phone_number, body_text, buttons, phone_number_id=None):
        captured.append(body_text)
        return True

    async def _capture_list(to_phone_number, header_text, body_text, button_text, sections, phone_number_id=None):
        captured.append(body_text)
        return True

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _capture_text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _capture_button)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _capture_list)

    return captured
