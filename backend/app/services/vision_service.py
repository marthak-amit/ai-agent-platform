"""
Groq vision service for analyzing product images sent by WhatsApp/Instagram customers.

Uses llama-4-scout to match customer-sent images against the client's catalogue
and reply in Hindi/Hinglish.

Channel differences:
- WhatsApp: sends media_id → two-step download (resolve URL then fetch bytes)
- Instagram: sends image URL directly → single fetch with Bearer token
Both channels pass image data to analyze_product_image() the same way.
"""

import base64
import logging

import httpx
from groq import AsyncGroq

from app.config import get_settings

logger = logging.getLogger(__name__)


async def download_whatsapp_media(media_id: str) -> bytes:
    """
    Download image bytes for a WhatsApp media_id.

    Step 1: GET graph.facebook.com/v21.0/{media_id} to resolve the CDN URL.
    Step 2: Fetch the actual image bytes from that URL.

    Args:
        media_id: The media_id string from the webhook payload.

    Returns:
        Raw image bytes.

    Raises:
        httpx.HTTPStatusError: If either Meta API call fails.
    """
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Resolve CDN URL
        meta_resp = await client.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers=headers,
        )
        meta_resp.raise_for_status()
        cdn_url = meta_resp.json()["url"]

        # Download image bytes
        img_resp = await client.get(cdn_url, headers=headers)
        img_resp.raise_for_status()
        return img_resp.content


async def download_instagram_media(image_url: str) -> bytes:
    """
    Download image bytes from an Instagram CDN URL.

    Unlike WhatsApp, Instagram includes the image URL directly in the webhook
    payload — no extra resolution step required. The URL still needs the
    Instagram access token as a Bearer header to succeed.

    Args:
        image_url: The CDN URL from message.attachments[0].payload.url.

    Returns:
        Raw image bytes.

    Raises:
        httpx.HTTPStatusError: If the download fails.
    """
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.instagram_access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(image_url, headers=headers)
        resp.raise_for_status()
        return resp.content


async def analyze_product_image(image_source: bytes | str, catalogue_context: str) -> str:
    """
    Send image to Groq llama-4-scout and return a Hindi/Hinglish reply.

    Accepts either raw bytes (WhatsApp path, after download) or a URL string
    (passed through directly when the caller has a public URL). Both formats
    are converted to the image_url content block Groq expects.

    Args:
        image_source:      Raw bytes of the image, or a URL string.
        catalogue_context: Formatted product catalogue string from catalogue_service.

    Returns:
        Natural-language reply string in Hindi/Hinglish.
    """
    settings = get_settings()
    client = AsyncGroq(api_key=settings.groq_api_key)

    if isinstance(image_source, str):
        image_content = {
            "type": "image_url",
            "image_url": {"url": image_source},
        }
    else:
        image_b64 = base64.b64encode(image_source).decode()
        image_content = {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        }

    response = await client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    image_content,
                    {
                        "type": "text",
                        "text": (
                            "You are a textile product assistant. NEVER reply with a dead-end "
                            "like \"no product code in shared media\" — that frustrates customers.\n\n"
                            f"Available products in catalogue:\n{catalogue_context}\n\n"
                            "Customer sent this image (likely asking 'is this available?' or "
                            "'how much is this?'). Try to identify which catalogue product matches "
                            "or closely resembles it — by category, style, color, fabric. "
                            "Reply in Hindi/Hinglish naturally, in this style:\n\n"
                            "IF a confident match is found:\n"
                            "  \"Ye to lagta hai humara [Product Name] [SKU] hai — ₹[Price] mein "
                            "available hai. Confirm karu availability?\"\n\n"
                            "IF no confident match, suggest 2-3 similar products from the catalogue "
                            "by category/style instead of saying nothing matches:\n"
                            "  \"Exact match toh nahi mila, lekin ye similar options hain:\n"
                            "  1. [Product 1] — ₹[Price]\n"
                            "  2. [Product 2] — ₹[Price]\n"
                            "  Inme se kisi mein interested hain?\"\n\n"
                            "Always end with a question that moves the conversation toward an order."
                        ),
                    },
                ],
            }
        ],
        max_tokens=300,
    )
    return response.choices[0].message.content
