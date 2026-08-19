"""
WhatsApp adapter — translates a channel-neutral PipelineResult ("SendInstruction")
into gated outbound.* sends (send_gate-checked wrappers over the raw
whatsapp_service transport).

SLICE 8 of the webhook.py strangler-fig refactor. This is the ONLY place
outbound.send_text / send_buttons / send_list
are called for the main reply path — order_pipeline.py decides WHAT to send
(buttons vs list vs text, which buttons, what body text) as plain data;
this module decides HOW to actually deliver that on WhatsApp (nonce encoding,
phone_number_id, the send_button_message → fallback-to-text behavior).

A future Instagram adapter would receive the same PipelineResult and instead
call the IG send API — translating `buttons` (≤3 items) to IG quick-replies,
and `list_options` (no native list UI on IG) to a numbered text fallback —
with no nonce concept needed at all, since IG's tap payload already carries
enough context.
"""

from __future__ import annotations

import logging

from app.services import outbound
from app.services.send_gate import MessageKind
from app.services.order_pipeline import PipelineResult, _encode_btn, _rotate_nonce

logger = logging.getLogger("app.routers.webhook")


async def send_pipeline_result(
    result: PipelineResult,
    *,
    db,
    conv,
    sender_phone: str,
    pid: str | None,
) -> None:
    """
    Send *result* via the WhatsApp Cloud API, mirroring webhook.py's original
    inline button_type dispatch exactly:

    - No buttons/list_options at all (or no phone_number_id available) → plain text.
    - buttons present + pid available → send_button_message with nonce-encoded
      ids; on failure (or no `sent` truthy result), fall back to plain text.
    - list_options present + pid available → send_list_message with
      nonce-encoded row ids; same fallback-to-text behavior.

    Nonce rotation happens here (not in order_pipeline.py) because it is a
    WhatsApp-specific anti-replay mechanism for buttons that stay tappable
    forever — the channel-neutral pipeline only ever supplies raw action ids.
    """
    if result.skip_send:
        return

    _gate_kw = dict(
        kind=MessageKind.PIPELINE_REPLY,
        db=db,
        client_id=getattr(conv, "client_id", None),
        conversation_id=conv.id,
    )

    for _pre_img_url, _pre_img_caption in (result.pre_images or []):
        try:
            await outbound.send_image(sender_phone, _pre_img_url, _pre_img_caption, **_gate_kw)
        except Exception as _pre_img_exc:
            logger.error("Pre-text image send error (non-fatal): %s", _pre_img_exc)

    for _pre in (result.pre_texts or []):
        try:
            await outbound.send_text(sender_phone, _pre, **_gate_kw)
        except Exception:
            pass

    needs_nonce = bool(result.buttons or result.list_options)
    nonce: str | None = None
    if needs_nonce and pid:
        nonce = await _rotate_nonce(db, conv.id, conv)

    def _nb(action: str) -> str:
        if nonce:
            return _encode_btn(action, conv.id, nonce)
        return action

    if result.buttons and pid:
        sent = await outbound.send_buttons(
            sender_phone,
            result.text,
            [{"id": _nb(b.id), "title": b.title} for b in result.buttons],
            phone_number_id=pid,
            **_gate_kw,
        )
        if not sent:
            await outbound.send_text(sender_phone, result.text, **_gate_kw)
    elif result.list_options and pid:
        sent = await outbound.send_list(
            sender_phone,
            result.list_header or "Choose an option",
            result.text,
            result.list_button_text or "View options",
            [{
                "title": "Options",
                "rows": [
                    {
                        "id": _nb(r.id),
                        "title": r.title,
                        "description": r.description or "",
                    }
                    for r in result.list_options
                ],
            }],
            phone_number_id=pid,
            **_gate_kw,
        )
        if not sent:
            await outbound.send_text(sender_phone, result.text, **_gate_kw)
    else:
        await outbound.send_text(sender_phone, result.text, **_gate_kw)

    # Deferred product images — sent AFTER the main text/buttons/list, mirroring
    # webhook.py's original end-of-request image queue (a failed image send must
    # never abort the pin path or block the text, which has already been sent).
    for _img_url, _img_caption in (result.images or []):
        try:
            await outbound.send_image(
                sender_phone, _img_url, _img_caption, **_gate_kw,
            )
        except Exception as _img_exc:
            logger.error("Product image send error (non-fatal): %s", _img_exc)
