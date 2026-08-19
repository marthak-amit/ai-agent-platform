"""
Instagram Cloud API sender service — RAW transport layer.

Sends DMs, quick replies, product images, and comment replies via the Meta
Graph API.

IMPORTANT: every function here is module-private (`_raw_*`) on purpose.
Nothing in app/ may call these directly except app/services/outbound.py,
which wraps each one behind send_gate.check_send(). See
tests/test_send_gate_guard.py.
"""

import logging

import httpx

from app.config import get_settings

META_API_VERSION = "v21.0"
META_API_BASE_URL = "https://graph.facebook.com"

logger = logging.getLogger(__name__)


async def _raw_send_dm(ig_user_id: str, recipient_igsid: str, message_text: str) -> dict:
    """
    Send a direct message to an Instagram user.

    Args:
        ig_user_id:      The Instagram Business Account ID (from webhook entry.id).
        recipient_igsid: The Instagram-Scoped ID of the message recipient.
        message_text:    Text content of the DM.

    Returns:
        Parsed JSON response from Meta API.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Meta API.
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{ig_user_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


async def _raw_send_quick_replies(
    ig_user_id: str, recipient_igsid: str, message_text: str, quick_replies: list[dict]
) -> bool:
    """
    Send a text message with up to 13 quick-reply chips.

    IG quick replies are text-only (content_type "text") — no images, same
    as the WhatsApp button send. Titles are truncated to 20 chars, matching
    the Messenger-platform display limit.

    Args:
        ig_user_id:      The Instagram Business Account ID.
        recipient_igsid: The Instagram-Scoped ID of the message recipient.
        message_text:    Body text shown above the quick replies.
        quick_replies:   List of {"id": str, "title": str} dicts (≤13 used).

    Returns:
        True if Meta accepted the message (HTTP 200), False otherwise.
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{ig_user_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {
            "text": message_text,
            "quick_replies": [
                {
                    "content_type": "text",
                    "title": qr["title"][:20],
                    "payload": qr["id"],
                }
                for qr in quick_replies[:13]
            ],
        },
        "messaging_type": "RESPONSE",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            logger.warning(
                "send_quick_replies failed %s: %s",
                response.status_code,
                response.text,
            )
        return response.status_code == 200


async def _raw_send_generic_template(
    ig_user_id: str, recipient_igsid: str, elements: list[dict]
) -> bool:
    """
    Send an IG Generic Template (carousel) message — up to 10 product cards,
    each with an image, title, subtitle, and a postback button.

    Bool-return, non-raising — same convention as _raw_send_quick_replies
    (as opposed to _raw_send_image/_raw_send_dm, which raise) so callers can
    fall back to plain text cleanly on any failure.

    Args:
        ig_user_id:      The Instagram Business Account ID.
        recipient_igsid: The Instagram-Scoped ID of the message recipient.
        elements:        List of ≤10 dicts: {"title", "subtitle", "image_url"
                         (optional), "buttons": [{"type": "postback",
                         "title": str, "payload": str}]}.

    Returns:
        True if Meta accepted the message (HTTP 200), False otherwise.
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{ig_user_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements[:10],
                },
            }
        },
        "messaging_type": "RESPONSE",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            logger.warning(
                "send_generic_template failed %s: %s",
                response.status_code,
                response.text,
            )
        return response.status_code == 200


async def _raw_send_image(ig_user_id: str, recipient_igsid: str, image_url: str) -> dict:
    """
    Send a product image (by public URL) to an Instagram user.

    IG's image attachment message has no caption field (unlike WhatsApp) —
    callers that need a caption should send it as a separate text message.

    Args:
        ig_user_id:      The Instagram Business Account ID.
        recipient_igsid: The Instagram-Scoped ID of the message recipient.
        image_url:       Publicly reachable URL of the image to send.

    Returns:
        Parsed JSON response from Meta API.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Meta API.
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{ig_user_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True},
            }
        },
        "messaging_type": "RESPONSE",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


async def _raw_reply_to_comment(
    ig_user_id: str, comment_id: str, message_text: str
) -> dict:
    """
    Post a public reply to an Instagram comment.

    Args:
        ig_user_id:   The Instagram Business Account ID.
        comment_id:   ID of the comment to reply to.
        message_text: Text of the reply.

    Returns:
        Parsed JSON response from Meta API.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Meta API.
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{comment_id}/replies"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json={"message": message_text})
        response.raise_for_status()
        return response.json()


async def _raw_send_private_reply(ig_user_id: str, comment_id: str, message_text: str) -> dict:
    """
    Send a Private Reply DM to a comment — Meta's mechanism for messaging a
    commenter with no existing DM thread (regular send_dm requires one).

    Same endpoint/payload shape as send_dm, but the recipient is addressed
    by comment_id instead of igsid — this is what actually opens the 24h
    messaging window from a comment, one DM per comment per Meta's limit.

    NOTE: verify this payload shape against a live Meta app before relying
    on it in production — this mirrors the documented mechanism but hasn't
    been smoke-tested against the real Graph API from this codebase.

    Args:
        ig_user_id:   The Instagram Business Account ID.
        comment_id:   ID of the comment that triggered this reply.
        message_text: Text content of the DM.

    Returns:
        Parsed JSON response from Meta API.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx from Meta API (e.g. commenter
            ineligible — private account, already messaged once, etc.).
    """
    settings = get_settings()
    url = f"{META_API_BASE_URL}/{META_API_VERSION}/{ig_user_id}/messages"

    headers = {
        "Authorization": f"Bearer {settings.instagram_access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"comment_id": comment_id},
        "message": {"text": message_text},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
