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

from app.services import outbound
from app.services.order_pipeline import PipelineResult
from app.services.send_gate import MessageKind

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
    db=None,
    conv=None,
) -> None:
    """
    Send *result* via the Instagram Graph API.

    - carousel_items present -> Generic Template (image + name + price per
      card, one "Select" postback per card); on failure, fall back to plain
      text (the numbered list already in result.text).
    - No buttons/list_options -> plain text.
    - buttons present, within the quick-reply cap -> send_quick_replies; on
      failure (or no `sent` truthy result), fall back to plain text.
    - list_options, or too many buttons for a quick reply -> numbered text
      fallback (matches the existing IG fallback intent — no native list UI).
    """
    if result.skip_send:
        return

    _gate_kw = dict(
        kind=MessageKind.PIPELINE_REPLY,
        db=db,
        client_id=getattr(conv, "client_id", None),
        conversation_id=getattr(conv, "id", None),
    )

    for _pre_img_url, _pre_img_caption in (result.pre_images or []):
        try:
            await outbound.ig_send_image(ig_user_id, recipient_igsid, _pre_img_url, **_gate_kw)
            if _pre_img_caption:
                await outbound.ig_send_dm(ig_user_id, recipient_igsid, _pre_img_caption, **_gate_kw)
        except Exception as _pre_img_exc:
            logger.error("Pre-text image send error (non-fatal): %s", _pre_img_exc)

    for _pre in (result.pre_texts or []):
        try:
            await outbound.ig_send_dm(ig_user_id, recipient_igsid, _pre, **_gate_kw)
        except Exception:
            pass

    if result.carousel_items:
        elements = [
            {
                "title": item.title,
                "subtitle": item.subtitle,
                **({"image_url": item.image_url} if item.image_url else {}),
                "buttons": [{"type": "postback", "title": "Select", "payload": item.sku}],
            }
            for item in result.carousel_items[:10]
        ]
        sent = await outbound.ig_send_generic_template(
            ig_user_id, recipient_igsid, elements, **_gate_kw,
        )
        if not sent:
            logger.info(
                "Multi-product reply: conv=%s route=text_fallback count=%s",
                getattr(conv, "id", None), len(result.carousel_items),
            )
            await outbound.ig_send_dm(ig_user_id, recipient_igsid, result.text, **_gate_kw)
    elif result.buttons and len(result.buttons) <= _IG_QUICK_REPLY_MAX:
        sent = await outbound.ig_send_quick_replies(
            ig_user_id, recipient_igsid, result.text,
            [{"id": b.id, "title": b.title} for b in result.buttons],
            **_gate_kw,
        )
        if not sent:
            await outbound.ig_send_dm(ig_user_id, recipient_igsid, result.text, **_gate_kw)
    elif result.buttons:
        # More buttons than IG quick replies support — numbered text fallback.
        await outbound.ig_send_dm(
            ig_user_id, recipient_igsid,
            _numbered_text_fallback(result.text, result.buttons),
            **_gate_kw,
        )
    elif result.list_options:
        await outbound.ig_send_dm(
            ig_user_id, recipient_igsid,
            _numbered_text_fallback(result.text, result.list_options),
            **_gate_kw,
        )
    else:
        await outbound.ig_send_dm(ig_user_id, recipient_igsid, result.text, **_gate_kw)

    # Deferred product images — sent AFTER the main text/quick-replies,
    # mirroring the WhatsApp adapter's deferred image queue.
    for _img_url, _img_caption in (result.images or []):
        try:
            await outbound.ig_send_image(ig_user_id, recipient_igsid, _img_url, **_gate_kw)
            if _img_caption:
                await outbound.ig_send_dm(ig_user_id, recipient_igsid, _img_caption, **_gate_kw)
        except Exception as _img_exc:
            logger.error("Product image send error (non-fatal): %s", _img_exc)
