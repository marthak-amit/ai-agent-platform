"""
Instagram adapter — translates a channel-neutral PipelineResult ("SendInstruction")
into the exact instagram_service calls instagram.py now needs for the main
order-pipeline reply path.

Mirrors app/routers/_whatsapp_adapter.py: order_pipeline.py decides WHAT to
send (buttons vs list vs text, which buttons, what body text) as plain data;
this module decides HOW to actually deliver that on Instagram.

IG has no native list UI and no nonce concept (a tap's payload already
carries the raw action id, so there's no replay window to defend against
the way WhatsApp's tappable-forever buttons have). Differences from the
WhatsApp adapter:

- buttons (≤13 items, IG's quick-reply cap) -> IG quick replies.
- list_options, or any choice set IG quick replies can't represent
  (>13 options) -> numbered text fallback (no native list UI on IG).
- No nonce encoding/rotation at all.
"""

from __future__ import annotations

import logging

from app.services import instagram_service
from app.services.order_pipeline import PipelineResult

logger = logging.getLogger("app.routers.instagram")

_IG_QUICK_REPLY_MAX = 13


def _numbered_text_fallback(body_text: str, options: list) -> str:
    """Render `options` (buttons or list_options) as a numbered text list."""
    lines = [body_text, ""]
    for i, opt in enumerate(options, start=1):
        lines.append(f"{i}. {opt.title}")
    return "\n".join(lines)


async def send_pipeline_result(
    result: PipelineResult,
    *,
    ig_user_id: str,
    recipient_igsid: str,
) -> None:
    """
    Send *result* via the Instagram Graph API.

    - No buttons/list_options -> plain text.
    - buttons present, within the quick-reply cap -> send_quick_replies; on
      failure (or no `sent` truthy result), fall back to plain text.
    - list_options, or too many buttons for a quick reply -> numbered text
      fallback (matches the existing IG fallback intent — no native list UI).
    """
    if result.skip_send:
        return

    for _pre in (result.pre_texts or []):
        try:
            await instagram_service.send_dm(ig_user_id, recipient_igsid, _pre)
        except Exception:
            pass

    if result.buttons and len(result.buttons) <= _IG_QUICK_REPLY_MAX:
        sent = await instagram_service.send_quick_replies(
            ig_user_id, recipient_igsid, result.text,
            quick_replies=[{"id": b.id, "title": b.title} for b in result.buttons],
        )
        if not sent:
            await instagram_service.send_dm(ig_user_id, recipient_igsid, result.text)
    elif result.buttons:
        # More buttons than IG quick replies support — numbered text fallback.
        await instagram_service.send_dm(
            ig_user_id, recipient_igsid,
            _numbered_text_fallback(result.text, result.buttons),
        )
    elif result.list_options:
        await instagram_service.send_dm(
            ig_user_id, recipient_igsid,
            _numbered_text_fallback(result.text, result.list_options),
        )
    else:
        await instagram_service.send_dm(ig_user_id, recipient_igsid, result.text)

    # Deferred product images — sent AFTER the main text/quick-replies,
    # mirroring the WhatsApp adapter's deferred image queue.
    for _img_url, _img_caption in (result.images or []):
        try:
            await instagram_service.send_image(ig_user_id, recipient_igsid, _img_url)
            if _img_caption:
                await instagram_service.send_dm(ig_user_id, recipient_igsid, _img_caption)
        except Exception as _img_exc:
            logger.error("Product image send error (non-fatal): %s", _img_exc)
