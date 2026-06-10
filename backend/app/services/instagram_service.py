"""
Instagram Cloud API sender service.

Sends DMs and comment replies via the Meta Graph API.
Used by the Instagram webhook handler for the comment-to-DM flow.
"""

import httpx

from app.config import get_settings

META_API_VERSION = "v21.0"
META_API_BASE_URL = "https://graph.facebook.com"


async def send_dm(ig_user_id: str, recipient_igsid: str, message_text: str) -> dict:
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


async def reply_to_comment(
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
