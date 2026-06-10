"""
WhatsApp Cloud API sender service.

Sends text, image, and interactive (button/list) messages via the
Meta Cloud API POST /v21.0/{phone_number_id}/messages endpoint.
"""

import logging

import httpx

from app.config import get_settings

META_API_VERSION = "v21.0"
META_API_BASE_URL = "https://graph.facebook.com"

logger = logging.getLogger(__name__)


async def send_text_message(to_phone_number: str, message_text: str) -> dict:
    """
    Send a plain-text WhatsApp message to a recipient.

    Constructs the Meta Cloud API request payload and posts it
    using an async httpx client. The access token and phone number
    ID are sourced from environment variables.

    Args:
        to_phone_number: Recipient phone number in E.164 format without '+',
                         e.g. "919876543210".
        message_text:    The text content to send.

    Returns:
        The parsed JSON response dict from Meta API on success.

    Raises:
        httpx.HTTPStatusError: If Meta API returns a 4xx or 5xx response.
    """
    settings = get_settings()

    url = (
        f"{META_API_BASE_URL}/{META_API_VERSION}"
        f"/{settings.whatsapp_phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone_number,
        "type": "text",
        "text": {"preview_url": False, "body": message_text},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if not response.is_success:
            logger.error(
                "WhatsApp send_text_message failed: status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
        response.raise_for_status()
        return response.json()


async def send_button_message(
    to_phone_number: str,
    body_text: str,
    buttons: list[dict],
    phone_number_id: str | None = None,
) -> bool:
    """
    Send a WhatsApp interactive button message (max 3 buttons).

    Falls back to the configured phone_number_id if none is passed.
    Button titles are silently truncated to Meta's 20-character limit.

    Args:
        to_phone_number: Recipient E.164 phone without '+'.
        body_text:       Main message body shown above the buttons.
        buttons:         List of dicts: [{"id": str, "title": str}, ...]
                         Maximum 3 buttons; extras are dropped.
        phone_number_id: Override the configured WhatsApp phone number ID.

    Returns:
        True if Meta accepted the message (HTTP 200), False otherwise.
    """
    settings = get_settings()
    pid = phone_number_id or settings.whatsapp_phone_number_id
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": btn["id"],
                            "title": btn["title"][:20],
                        },
                    }
                    for btn in buttons[:3]
                ]
            },
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            logger.warning(
                "send_button_message failed %s: %s",
                response.status_code,
                response.text,
            )
        return response.status_code == 200


async def send_list_message(
    to_phone_number: str,
    header_text: str,
    body_text: str,
    button_text: str,
    sections: list[dict],
    phone_number_id: str | None = None,
) -> bool:
    """
    Send a WhatsApp interactive list message (scrollable item picker).

    Args:
        to_phone_number: Recipient E.164 phone without '+'.
        header_text:     Bold header shown above the body.
        body_text:       Description text above the list button.
        button_text:     Label on the button that opens the list (max 20 chars).
        sections:        List of section dicts:
                         [{"title": str, "rows": [{"id": str, "title": str,
                           "description": str}, ...]}, ...]
        phone_number_id: Override the configured WhatsApp phone number ID.

    Returns:
        True if Meta accepted the message (HTTP 200), False otherwise.
    """
    settings = get_settings()
    pid = phone_number_id or settings.whatsapp_phone_number_id
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header_text},
            "body": {"text": body_text},
            "action": {
                "button": button_text[:20],
                "sections": sections,
            },
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            logger.warning(
                "send_list_message failed %s: %s",
                response.status_code,
                response.text,
            )
        return response.status_code == 200


async def send_image_message(to_phone_number: str, image_url: str, caption: str | None = None) -> dict:
    """
    Send an image WhatsApp message (by public URL) to a recipient.

    Used for product photos — e.g. when a customer shares a SKU and the
    matching catalogue product has an image_url. Should NOT be used for
    unsolicited brand/logo images.

    Args:
        to_phone_number: Recipient phone number in E.164 format without '+'.
        image_url:       Publicly reachable URL of the image to send.
        caption:         Optional caption text shown under the image.

    Returns:
        The parsed JSON response dict from Meta API on success.

    Raises:
        httpx.HTTPStatusError: If Meta API returns a 4xx or 5xx response.
    """
    settings = get_settings()

    url = (
        f"{META_API_BASE_URL}/{META_API_VERSION}"
        f"/{settings.whatsapp_phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    image_payload: dict = {"link": image_url}
    if caption:
        image_payload["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone_number,
        "type": "image",
        "image": image_payload,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
