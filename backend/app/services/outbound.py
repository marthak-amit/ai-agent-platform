"""
Outbound send wrappers — the ONLY module allowed to call the raw Meta/IG
transport functions (`whatsapp_service._raw_*` / `instagram_service._raw_*`).

Every function here:
  1. resolves the Customer row for the recipient (when db/client_id given),
  2. asks send_gate.check_send() for a verdict,
  3. refuses on DENY, and — because no approved Meta template exists yet —
     also refuses on ALLOW_TEMPLATE_ONLY (logging the suppression), sending
     ONLY on a full free-form ALLOW,
  4. performs the raw send.

When template-send support is built, it gets its own wrapper here that
accepts ALLOW_TEMPLATE_ONLY; free-form senders never will.

Suppressed sends return None (dict-returning senders) or False
(bool-returning senders) — they never raise, so callers' existing
try/except blocks keep meaning "transport error", not "policy refusal".
tests/test_send_gate_guard.py enforces that no other module bypasses this.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.services import instagram_service, whatsapp_service
from app.services.send_gate import (
    DenyReason,
    MessageKind,
    SendDecision,
    Verdict,
    check_send,
    log_suppression,
)

logger = logging.getLogger(__name__)


async def _gate(
    db,
    client_id: int | None,
    recipient: str,
    kind: MessageKind,
    channel: str,
    conversation_id: int | None,
    customer,
    is_optout_confirmation: bool = False,
    comment_created_at: datetime | None = None,
):
    """
    Resolve the customer row (if possible) and run check_send().

    Returns (decision, customer). A free-form send may proceed only when
    decision.allowed — ALLOW_TEMPLATE_ONLY is converted to a logged refusal
    here because no approved template exists yet.
    """
    if customer is None and db is not None and client_id is not None:
        try:
            from app.models.customer import Customer
            from app.services import customer_service

            customer = await customer_service.get_customer(db, client_id, recipient)
            if not isinstance(customer, Customer):
                # Defensive: only a real Customer row may drive gate decisions.
                customer = None
        except Exception as exc:
            logger.warning("outbound: customer lookup failed for %s: %s", recipient, exc)
            customer = None

    decision: SendDecision = await check_send(
        db,
        client_id=client_id,
        customer=customer,
        message_kind=kind,
        channel=channel,
        conversation_id=conversation_id,
        is_optout_confirmation=is_optout_confirmation,
        comment_created_at=comment_created_at,
    )

    if decision.verdict is Verdict.ALLOW_TEMPLATE_ONLY:
        # Free-form path with the 24h window closed: Meta only accepts an
        # approved template here, and none is built yet — never attempt the
        # free-form send. This is the fix for the follow-up-engine bug.
        log_suppression(
            customer_id=getattr(customer, "id", None),
            client_id=client_id if client_id is not None else getattr(customer, "client_id", None),
            message_kind=kind,
            channel=channel,
            reason=DenyReason.WINDOW_CLOSED,
            detail="no_template_available",
        )
        return SendDecision(Verdict.DENY, DenyReason.NO_TEMPLATE_AVAILABLE), customer

    return decision, customer


async def _stamp_optout_confirmed(db, customer) -> None:
    """Mark the one-time opt-out confirmation as used (DB-enforced one-shot)."""
    if db is None or customer is None:
        return
    try:
        customer.optout_confirmed_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        logger.error("outbound: failed to stamp optout_confirmed_at: %s", exc)


# ─── WhatsApp ────────────────────────────────────────────────────────────────

async def send_typing_indicator(
    wamid: str,
    phone_number_id: str | None = None,
) -> dict | None:
    """
    Mark an inbound WhatsApp message read and show "typing..." to its sender.

    Bypasses send_gate.check_send() on purpose: this isn't a business-
    initiated message, it's a read receipt + typing cue tied to a specific
    inbound wamid the customer just sent us. Meta exempts it from the 24h
    window, and it carries no opt-out/marketing content to police. Never
    raises — a transport failure here must not affect the real reply.
    """
    try:
        return await whatsapp_service._raw_send_typing_indicator(
            wamid=wamid, phone_number_id=phone_number_id
        )
    except Exception as exc:
        logger.debug("send_typing_indicator failed for wamid=%s: %s", wamid, exc)
        return None


async def send_text(
    to_phone_number: str,
    message_text: str,
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
    is_optout_confirmation: bool = False,
) -> dict | None:
    """Gated WhatsApp plain-text send. Returns Meta's response, or None if suppressed."""
    decision, customer = await _gate(
        db, client_id, to_phone_number, kind, "whatsapp",
        conversation_id, customer, is_optout_confirmation,
    )
    if not decision.allowed:
        return None
    result = await whatsapp_service._raw_send_text_message(
        to_phone_number=to_phone_number, message_text=message_text
    )
    if is_optout_confirmation:
        await _stamp_optout_confirmed(db, customer)
    return result


async def send_buttons(
    to_phone_number: str,
    body_text: str,
    buttons: list[dict],
    *,
    kind: MessageKind,
    phone_number_id: str | None = None,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> bool:
    """Gated WhatsApp interactive-button send. Returns False if suppressed or rejected."""
    decision, customer = await _gate(
        db, client_id, to_phone_number, kind, "whatsapp", conversation_id, customer,
    )
    if not decision.allowed:
        return False
    return await whatsapp_service._raw_send_button_message(
        to_phone_number=to_phone_number, body_text=body_text, buttons=buttons,
        phone_number_id=phone_number_id,
    )


async def send_list(
    to_phone_number: str,
    header_text: str,
    body_text: str,
    button_text: str,
    sections: list[dict],
    *,
    kind: MessageKind,
    phone_number_id: str | None = None,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> bool:
    """Gated WhatsApp interactive-list send. Returns False if suppressed or rejected."""
    decision, customer = await _gate(
        db, client_id, to_phone_number, kind, "whatsapp", conversation_id, customer,
    )
    if not decision.allowed:
        return False
    return await whatsapp_service._raw_send_list_message(
        to_phone_number=to_phone_number, header_text=header_text, body_text=body_text,
        button_text=button_text, sections=sections, phone_number_id=phone_number_id,
    )


async def send_image(
    to_phone_number: str,
    image_url: str,
    caption: str | None = None,
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> dict | None:
    """Gated WhatsApp image send. Returns Meta's response, or None if suppressed."""
    decision, customer = await _gate(
        db, client_id, to_phone_number, kind, "whatsapp", conversation_id, customer,
    )
    if not decision.allowed:
        return None
    return await whatsapp_service._raw_send_image_message(
        to_phone_number=to_phone_number, image_url=image_url, caption=caption
    )


async def send_document(
    to_phone_number: str,
    document_url: str,
    filename: str,
    caption: str | None = None,
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> dict | None:
    """Gated WhatsApp document send (PDF invoices). None if suppressed."""
    decision, customer = await _gate(
        db, client_id, to_phone_number, kind, "whatsapp", conversation_id, customer,
    )
    if not decision.allowed:
        return None
    return await whatsapp_service._raw_send_document_message(
        to_phone_number=to_phone_number, document_url=document_url,
        filename=filename, caption=caption,
    )


async def send_owner_text(
    to_phone_number: str,
    message_text: str,
    *,
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> dict | None:
    """
    Platform → seller notification (escalations, payment alerts, briefings,
    channel-setup test message).

    The recipient is the client, not a customer, so opt-out/window rules
    don't apply (OWNER_ALERT is ALLOW by rule 1) — routed through the gate
    anyway so the choke-point invariant holds everywhere. Per-client
    credentials may be passed for sends on the client's own WABA.
    """
    decision = await check_send(None, message_kind=MessageKind.OWNER_ALERT, channel="whatsapp")
    if not decision.allowed:  # defensive; rule 1 always allows today
        return None
    kwargs: dict = {}
    if phone_number_id:
        kwargs["phone_number_id"] = phone_number_id
    if access_token:
        kwargs["access_token"] = access_token
    return await whatsapp_service._raw_send_text_message(
        to_phone_number=to_phone_number, message_text=message_text, **kwargs
    )


# ─── Instagram ───────────────────────────────────────────────────────────────

async def ig_send_dm(
    ig_user_id: str,
    recipient_igsid: str,
    message_text: str,
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
    is_optout_confirmation: bool = False,
) -> dict | None:
    """Gated Instagram DM send. Returns Meta's response, or None if suppressed."""
    decision, customer = await _gate(
        db, client_id, recipient_igsid, kind, "instagram",
        conversation_id, customer, is_optout_confirmation,
    )
    if not decision.allowed:
        return None
    result = await instagram_service._raw_send_dm(
        ig_user_id=ig_user_id, recipient_igsid=recipient_igsid, message_text=message_text
    )
    if is_optout_confirmation:
        await _stamp_optout_confirmed(db, customer)
    return result


async def ig_send_quick_replies(
    ig_user_id: str,
    recipient_igsid: str,
    message_text: str,
    quick_replies: list[dict],
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> bool:
    """Gated Instagram quick-replies send. Returns False if suppressed or rejected."""
    decision, customer = await _gate(
        db, client_id, recipient_igsid, kind, "instagram", conversation_id, customer,
    )
    if not decision.allowed:
        return False
    return await instagram_service._raw_send_quick_replies(
        ig_user_id=ig_user_id, recipient_igsid=recipient_igsid,
        message_text=message_text, quick_replies=quick_replies,
    )


async def ig_send_image(
    ig_user_id: str,
    recipient_igsid: str,
    image_url: str,
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> dict | None:
    """Gated Instagram image send. Returns Meta's response, or None if suppressed."""
    decision, customer = await _gate(
        db, client_id, recipient_igsid, kind, "instagram", conversation_id, customer,
    )
    if not decision.allowed:
        return None
    return await instagram_service._raw_send_image(
        ig_user_id=ig_user_id, recipient_igsid=recipient_igsid, image_url=image_url
    )


async def ig_send_generic_template(
    ig_user_id: str,
    recipient_igsid: str,
    elements: list[dict],
    *,
    kind: MessageKind,
    db=None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    customer=None,
) -> bool:
    """Gated IG Generic Template (carousel) send. Returns False if suppressed, rejected, or failed."""
    decision, customer = await _gate(
        db, client_id, recipient_igsid, kind, "instagram", conversation_id, customer,
    )
    if not decision.allowed:
        return False
    return await instagram_service._raw_send_generic_template(
        ig_user_id=ig_user_id, recipient_igsid=recipient_igsid, elements=elements
    )


async def ig_send_private_reply(
    ig_user_id: str,
    comment_id: str,
    message_text: str,
    *,
    db=None,
    client_id: int | None = None,
    recipient_igsid: str | None = None,
    comment_created_at: datetime | None = None,
) -> dict | None:
    """
    Gated Instagram Private Reply (DM to a commenter with no DM thread).

    Meta's rule for this API differs from the DM window: one reply per
    comment (uniqueness tracked by ig_comment_service in ig_comment_replies),
    within 7 days of the comment — enforced by the gate via
    channel="instagram_comment".
    """
    decision, _customer = await _gate(
        db, client_id, recipient_igsid or comment_id,
        MessageKind.PIPELINE_REPLY, "instagram_comment",
        None, None, comment_created_at=comment_created_at,
    )
    if not decision.allowed:
        return None
    return await instagram_service._raw_send_private_reply(
        ig_user_id=ig_user_id, comment_id=comment_id, message_text=message_text
    )


async def ig_reply_to_comment(
    ig_user_id: str, comment_id: str, message_text: str
) -> dict:
    """
    Public comment reply (not a DM — visible under the post).

    No messaging window or opt-out applies to public comment replies; kept
    here so ALL Graph API writes flow through the one module the CI guard
    audits.
    """
    return await instagram_service._raw_reply_to_comment(
        ig_user_id=ig_user_id, comment_id=comment_id, message_text=message_text
    )
