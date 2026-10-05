"""
Channel-neutral sender for messages that originate OUTSIDE the inbound
pipeline: payment approve/reject/cancel templates and dashboard (human) sends.

Everything goes through app/services/outbound.py — the one module allowed to
touch the Meta transports — so opt-out, block and 24h-window policy is
enforced by send_gate exactly as for bot replies. This module adds:
  * WhatsApp vs Instagram routing from conversation.channel,
  * persistence of the outbound message (direction/sender_type/media),
  * a realtime `new_message` event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.conversation import Conversation
from app.services import conversation_service, outbound, realtime_service, send_gate
from app.services.send_gate import MessageKind, Verdict

logger = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    """
    Result of a channel send.

    sent:    True when Meta accepted the message and it was persisted.
    reason:  machine-readable failure code when sent is False —
             'WINDOW_CLOSED' | 'OPTED_OUT' | 'BLOCKED' | 'SUPPRESSED' |
             'NO_CHANNEL' | 'SEND_FAILED'.
    message: the persisted Message row when sent.
    """

    sent: bool
    reason: str | None = None
    message: object | None = None


def message_to_dict(m) -> dict:
    """Serialise a Message row for API/realtime payloads."""
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "direction": m.direction or ("inbound" if m.role == "user" else "outbound"),
        "sender_type": m.sender_type or ("customer" if m.role == "user" else "bot"),
        "sender_user_id": m.sender_user_id,
        "channel": m.channel,
        "text": m.content,
        "media_url": m.media_url,
        "media_type": m.media_type,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def precheck(db, client, conv: Conversation, kind: MessageKind) -> str | None:
    """
    Dry-run the send gate for this conversation.

    Returns None when a free-form send is allowed, else the failure code
    ('WINDOW_CLOSED', 'OPTED_OUT', 'BLOCKED').
    """
    from app.services import customer_service

    customer = await customer_service.get_customer(db, client.id, conv.phone_number)
    decision = await send_gate.check_send(
        db,
        client_id=client.id,
        customer=customer,
        message_kind=kind,
        channel=conv.channel,
        conversation_id=conv.id,
    )
    if decision.verdict is Verdict.ALLOW:
        return None
    if decision.verdict is Verdict.ALLOW_TEMPLATE_ONLY:
        return "WINDOW_CLOSED"
    reason = getattr(decision.reason, "value", None)
    return {"opted_out": "OPTED_OUT", "blocked": "BLOCKED"}.get(reason, "SUPPRESSED")


async def send_to_conversation(
    db,
    client,
    conv: Conversation,
    *,
    text: str | None = None,
    image_url: str | None = None,
    kind: MessageKind,
    sender_type: str,
    sender_user_id: int | None = None,
    persisted_media_url: str | None = None,
) -> SendOutcome:
    """
    Send text and/or one image to the customer of `conv`, then persist it.

    Args:
        db:                  Async session.
        client:              Owning Client row (credentials + ids).
        conv:                Target Conversation (channel decides WA vs IG).
        text:                Text body, or the image caption when image_url is set.
        image_url:           Publicly fetchable image URL to send.
        kind:                MessageKind for the gate (UTILITY_TEMPLATE for
                             payment templates, MANUAL_AGENT for dashboard sends).
        sender_type:         'bot' | 'human' | 'system' (stored on the message).
        sender_user_id:      Staff user id when sender_type == 'human'.
        persisted_media_url: URL stored on the message row (defaults to image_url).

    Returns:
        SendOutcome. Never raises on policy suppression; transport errors are
        reported as reason='SEND_FAILED'.
    """
    if not text and not image_url:
        return SendOutcome(False, "SUPPRESSED")

    common = dict(kind=kind, db=db, client_id=client.id, conversation_id=conv.id)
    try:
        if conv.channel == "whatsapp":
            wa = dict(
                phone_number_id=getattr(client, "whatsapp_phone_number_id", None),
                access_token=getattr(client, "whatsapp_access_token", None),
            )
            if image_url:
                result = await outbound.send_image(
                    conv.phone_number, image_url, text, **common, **wa
                )
            else:
                result = await outbound.send_text(conv.phone_number, text, **common, **wa)
        elif conv.channel == "instagram":
            ig_id = getattr(client, "instagram_account_id", None)
            if not ig_id:
                return SendOutcome(False, "NO_CHANNEL")
            if image_url:
                result = await outbound.ig_send_image(ig_id, conv.phone_number, image_url, **common)
                if result is not None and text:
                    await outbound.ig_send_dm(ig_id, conv.phone_number, text, **common)
            else:
                result = await outbound.ig_send_dm(ig_id, conv.phone_number, text, **common)
        else:
            return SendOutcome(False, "NO_CHANNEL")
    except Exception as exc:
        logger.error("channel_sender: send failed conv=%s: %s", conv.id, exc)
        return SendOutcome(False, "SEND_FAILED")

    if result is None or result is False:
        return SendOutcome(False, "SUPPRESSED")

    msg = await conversation_service.save_message(
        db, conv.id, "assistant", text or "[image]",
        channel=conv.channel,
        sender_type=sender_type,
        sender_user_id=sender_user_id,
        media_url=persisted_media_url or image_url,
        media_type="image" if image_url else None,
    )
    realtime_service.publish(
        client.id, "new_message",
        {"conversation_id": conv.id, "message": message_to_dict(msg)},
    )
    return SendOutcome(True, None, msg)
