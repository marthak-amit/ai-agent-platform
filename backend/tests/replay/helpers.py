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

    async def _capture_text(to_phone_number, message_text, phone_number_id=None, access_token=None):
        captured.append(message_text)

    async def _capture_button(to_phone_number, body_text, buttons, phone_number_id=None, access_token=None):
        captured.append(body_text)
        return True

    async def _capture_list(
        to_phone_number, header_text, body_text, button_text, sections,
        phone_number_id=None, access_token=None,
    ):
        captured.append(body_text)
        return True

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _capture_text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _capture_button)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _capture_list)

    return captured


# 1x1 transparent PNG — enough bytes for the inbound-media pipeline (no vision model runs on proofs).
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def wa_image_payload(
    sender: str,
    *,
    media_id: str = "media.proof.1",
    wamid: str | None = None,
    phone_number_id: str = WA_PHONE_NUMBER_ID,
    ts: int | None = None,
) -> tuple[bytes, dict]:
    """
    Build a WhatsApp image-message webhook payload (a customer's payment screenshot).

    Returns (body_bytes, headers) ready to POST to /webhook.
    """
    wamid = wamid or f"wamid.img.{sender}.{time.time_ns()}"
    ts = ts or int(time.time())
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "ENTRY_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "0", "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
                    "messages": [{
                        "from": sender, "id": wamid, "timestamp": str(ts), "type": "image",
                        "image": {"id": media_id, "mime_type": "image/png"},
                    }],
                },
            }],
        }],
    }
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}


async def send_image(client, sender: str, *, media_id: str = "media.proof.1",
                     wamid: str | None = None, phone_number_id: str = WA_PHONE_NUMBER_ID):
    """POST a WhatsApp image message through the replay HTTP client."""
    body, headers = wa_image_payload(sender, media_id=media_id, wamid=wamid, phone_number_id=phone_number_id)
    return await client.post("/webhook", content=body, headers=headers)


def stub_media_and_capture_sends(monkeypatch) -> dict:
    """
    Make inbound media + outbound sends deterministic for payment-proof replays.

    * vision_service.download_whatsapp_media → TINY_PNG (no network)
    * media_service.store_media              → a stable fake CDN URL per upload
    * WhatsApp text/image sends return a Meta-shaped dict (outbound treats a
      None return as "suppressed") and are recorded.

    Returns {"texts": [...], "images": [(url, caption), ...]} — live lists.
    """
    import itertools
    import unittest.mock as mock

    counter = itertools.count(1)
    sent: dict = {"texts": [], "images": []}

    async def _download(media_id, *a, **kw):
        return TINY_PNG

    async def _store(client_id, data, content_type, folder="chat"):
        return f"https://cdn.test/{folder}/{next(counter)}.png"

    async def _text(to_phone_number, message_text, phone_number_id=None, access_token=None):
        sent["texts"].append(message_text)
        return {"messages": [{"id": f"wamid.out.{len(sent['texts'])}"}]}

    async def _image(to_phone_number, image_url, caption=None, phone_number_id=None, access_token=None):
        sent["images"].append((image_url, caption))
        return {"messages": [{"id": f"wamid.outimg.{len(sent['images'])}"}]}

    monkeypatch.setattr("app.services.vision_service.download_whatsapp_media", _download)
    monkeypatch.setattr("app.services.media_service.store_media", _store)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_image_message", _image)
    monkeypatch.setattr("app.services.ocr_service.detect_sku_from_image", lambda *_: None)
    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_button_message", mock.AsyncMock(return_value=True)
    )
    return sent


async def customer_pays_and_seller_approves(
    http, session, monkeypatch, *, phone: str, pnid: str, client_id: int, order_id: int,
):
    """
    Replay the NEW payment path end-to-end for tests that used to type "paid":
    the customer sends a screenshot (order → payment_submitted) and the seller
    approves it (order → paid, stock deducted once). Returns the DecisionResult.
    """
    from app.models.client import Client
    from app.services import payment_verification_service as pvs

    stub_media_and_capture_sends(monkeypatch)
    resp = await send_image(http, phone, phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    client = await session.get(Client, client_id)
    return await pvs.approve(session, client, order_id, None)


async def pay_latest_order(http, session, monkeypatch, *, phone: str, pnid: str):
    """
    Like customer_pays_and_seller_approves, but locates the customer's newest
    pending_payment order itself — a drop-in for the old "customer types
    'paid'" step in UPI flow tests.
    """
    from sqlalchemy import select

    from app.models.order import Order

    row = (await session.execute(
        select(Order).where(Order.customer_phone == phone, Order.status == "pending_payment")
        .order_by(Order.id.desc()).limit(1).execution_options(populate_existing=True)
    )).scalar_one()
    return await customer_pays_and_seller_approves(
        http, session, monkeypatch, phone=phone, pnid=pnid, client_id=row.client_id, order_id=row.id,
    )
