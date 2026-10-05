"""
Conversations API router (protected — requires JWT, scoped to the caller's business).

Powers the dashboard Chat Inbox: conversation list with unread/payment/bot
filters, full thread (with payment-proof cards and order events), human
send (text / image / approved template) with 24h-window enforcement, and
bot pause/resume.

Endpoints:
- GET   /conversations                     list (filter=all|unread|payment_to_verify|bot_paused, search=)
- GET   /conversations/{id}                thread + customer panel + 24h window state
- POST  /conversations/{id}/read           mark read (clears unread count)
- POST  /conversations/{id}/media          upload an image to attach to a message
- POST  /conversations/{id}/messages       send as a human {text | media_url | template}
- POST  /conversations/{id}/send-message   deprecated alias of /messages (text only)
- PATCH /conversations/{id}/takeover       pause the bot (manual)
- PATCH /conversations/{id}/resume         resume the bot
- GET   /conversations/templates/approved  approved utility templates usable outside the 24h window

Every route requires the `manual_reply` permission.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.message_template import MessageTemplate
from app.models.order import Order
from app.models.order_audit_log import OrderAuditLog
from app.models.payment_proof import PaymentProof
from app.models.user import User
from app.routers.auth import require_permission
from app.services import (
    channel_sender,
    conversation_control,
    media_service,
    outbound,
    send_gate,
)
from app.services.order_state_machine import ORDER_PAYMENT_SUBMITTED
from app.services.send_gate import MessageKind

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conversations", tags=["conversations"])

_perm = require_permission("manual_reply")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_THREAD_PAGE = 100


# ── Schemas ───────────────────────────────────────────────────────────────────

class LastMessageOut(BaseModel):
    """Preview of the newest message in a conversation."""

    text: str
    direction: str
    sender_type: str
    media_type: Optional[str]
    created_at: Optional[str]


class ConversationSummary(BaseModel):
    """Row in the inbox list."""

    id: int
    customer_name: Optional[str]
    phone_number: str
    channel: str
    lead_status: str
    last_message: Optional[LastMessageOut]
    unread_count: int
    payment_to_verify: bool
    bot_paused: bool
    ai_enabled: bool
    updated_at: Optional[str]


class ProofCardOut(BaseModel):
    """Payment-proof card attached to a screenshot bubble."""

    proof_id: int
    order_id: int
    order_number: str
    amount_expected: float
    status: str  # pending | approved | rejected
    order_status: str
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    reject_reason: Optional[str]


class MessageOut(BaseModel):
    """A single message in the thread."""

    id: int
    direction: str            # inbound | outbound
    sender_type: str          # customer | bot | human | system
    sender_name: Optional[str]
    text: str
    media_url: Optional[str]
    media_type: Optional[str]
    created_at: str
    payment_proof: Optional[ProofCardOut] = None
    # legacy field kept for the old Conversations page: user | assistant | system
    role: str = "user"
    content: str = ""


class OrderEventOut(BaseModel):
    """Inline order-status chip event."""

    order_id: int
    order_number: str
    status: str
    at: str


class OrderBrief(BaseModel):
    """Order row for the customer panel."""

    id: int
    order_number: str
    status: str
    total_amount: float
    created_at: Optional[str]
    items: list[str]


class WindowOut(BaseModel):
    """State of Meta's 24h free-form window."""

    open: bool
    closes_at: Optional[str]
    seconds_left: int


class ConversationDetail(BaseModel):
    """Full thread plus everything the three-column inbox needs."""

    id: int
    phone_number: str
    channel: str
    customer_name: Optional[str]
    ai_enabled: bool
    bot_paused: bool
    bot_pause_source: Optional[str]
    auto_resume_at: Optional[str]
    taken_over_at: Optional[str]
    taken_over_note: Optional[str]
    lead_status: str
    message_count: int
    created_at: str
    updated_at: Optional[str]
    window: WindowOut
    opted_out: bool
    messages: list[MessageOut]
    has_more: bool
    events: list[OrderEventOut]
    current_order: Optional[OrderBrief]
    orders: list[OrderBrief]


class TemplateOut(BaseModel):
    """An approved message template."""

    id: int
    name: str
    language: str
    category: str
    body: str


class TemplateSend(BaseModel):
    """Template reference for a send outside the 24h window."""

    name: str
    language: str = "en"
    variables: list[str] = Field(default_factory=list)


class SendRequest(BaseModel):
    """Body for POST /conversations/{id}/messages — exactly one of text/media_url/template."""

    text: Optional[str] = Field(default=None, max_length=4096)
    media_url: Optional[str] = Field(default=None, description="URL returned by POST /conversations/{id}/media.")
    caption: Optional[str] = Field(default=None, max_length=1024)
    template: Optional[TemplateSend] = None
    client_msg_id: Optional[str] = Field(
        default=None, max_length=64,
        description="Idempotency key: a retry with the same id never double-sends.",
    )

    @model_validator(mode="after")
    def _one_payload(self) -> "SendRequest":
        """Require a non-empty text, media_url or template."""
        if self.text is not None and not self.text.strip() and not self.media_url and not self.template:
            raise ValueError("text must not be blank")
        if not (self.text or self.media_url or self.template):
            raise ValueError("Provide text, media_url or template.")
        return self


class SendResult(BaseModel):
    """Result of a human send."""

    message: MessageOut
    bot_paused: bool
    auto_resume_at: Optional[str]


class TakeoverRequest(BaseModel):
    """Body for PATCH /conversations/{id}/takeover."""

    note: Optional[str] = None


class LegacySendRequest(BaseModel):
    """Body for the deprecated POST /conversations/{id}/send-message."""

    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to ISO string, or None."""
    return dt.isoformat() if dt else None


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce naive timestamps to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _get_conv_or_404(db: AsyncSession, client_id: int, conv_id: int) -> Conversation:
    """Fetch a Conversation scoped to the caller's business, or raise 404."""
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == conv_id, Conversation.client_id == client_id)
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "CONVERSATION_NOT_FOUND", "message": "Conversation not found."})
    return conv


def _effective_bot_paused(conv: Conversation, client) -> bool:
    """Paused, unless a human_send pause has already idled out (resumes on next inbound)."""
    return conv.ai_enabled is False and not conversation_control.auto_resume_due(conv, client)


async def _window(db: AsyncSession, conv: Conversation) -> WindowOut:
    """Compute the 24h window from the conversation's last inbound message."""
    last = _utc((await db.execute(
        select(func.max(Message.created_at)).where(Message.conversation_id == conv.id, Message.role == "user")
    )).scalar_one())
    if last is None:
        return WindowOut(open=False, closes_at=None, seconds_left=0)
    closes = last + timedelta(hours=send_gate.WINDOW_HOURS)
    left = int((closes - datetime.now(timezone.utc)).total_seconds())
    return WindowOut(open=left > 0, closes_at=closes.isoformat(), seconds_left=max(0, left))


async def _approved_templates(db: AsyncSession, client_id: int) -> list[TemplateOut]:
    """Approved utility templates for this business."""
    rows = (await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.client_id == client_id,
            MessageTemplate.status == "approved", MessageTemplate.category == "utility",
        ).order_by(MessageTemplate.name)
    )).scalars().all()
    return [TemplateOut(id=t.id, name=t.name, language=t.language, category=t.category, body=t.body) for t in rows]


def _msg_out(m: Message, conv_channel: str, proofs: dict[int, ProofCardOut], names: dict[int, str]) -> MessageOut:
    """Serialize a Message (+ optional proof card) for the thread."""
    d = channel_sender.message_to_dict(m)
    return MessageOut(
        id=m.id, direction=d["direction"], sender_type=d["sender_type"],
        sender_name=names.get(m.sender_user_id) if m.sender_user_id else None,
        text=m.content, media_url=m.media_url, media_type=m.media_type,
        created_at=_ts(m.created_at) or "", payment_proof=proofs.get(m.id),
        role="user" if d["direction"] == "inbound" else "assistant", content=m.content,
    )


def _order_brief(o: Order) -> OrderBrief:
    """Serialize an order for the customer panel."""
    items = [f"{li.product_name} × {li.quantity}" for li in (o.line_items or [])] or [f"{o.product_name} × {o.quantity}"]
    return OrderBrief(
        id=o.id, order_number=o.order_number, status=o.status,
        total_amount=o.total_amount, created_at=_ts(o.created_at), items=items,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/templates/approved", response_model=list[TemplateOut])
async def list_approved_templates(
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> list[TemplateOut]:
    """Approved utility templates usable when the 24h window is closed."""
    return await _approved_templates(db, user.client_id)


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    filter: Literal["all", "unread", "payment_to_verify", "bot_paused"] = Query("all"),
    search: Optional[str] = Query(None, max_length=100, description="Customer name or phone."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_perm),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationSummary]:
    """
    List the business's conversations, most recent activity first.

    Filters: unread (inbound newer than last_read_at), payment_to_verify
    (has an order in payment_submitted), bot_paused (AI not replying).
    """
    cid = user.client_id
    last_msg_at = (
        select(func.max(Message.created_at)).where(Message.conversation_id == Conversation.id).correlate(Conversation).scalar_subquery()
    )
    unread = (
        select(func.count(Message.id)).where(
            Message.conversation_id == Conversation.id, Message.role == "user",
            Message.created_at > func.coalesce(Conversation.last_read_at, _EPOCH),
        ).correlate(Conversation).scalar_subquery()
    )
    to_verify = exists().where(
        Order.conversation_id == Conversation.id, Order.status == ORDER_PAYMENT_SUBMITTED
    ).correlate(Conversation)

    stmt = (
        select(Conversation, Customer.name, unread.label("unread"), to_verify.label("to_verify"))
        .outerjoin(Customer, (Customer.client_id == Conversation.client_id) & (Customer.phone == Conversation.phone_number))
        .where(Conversation.client_id == cid, Conversation.is_sandbox == False)  # noqa: E712
    )
    if filter == "unread":
        stmt = stmt.where(unread > 0)
    elif filter == "payment_to_verify":
        stmt = stmt.where(to_verify)
    elif filter == "bot_paused":
        stmt = stmt.where(Conversation.ai_enabled == False)  # noqa: E712
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Conversation.phone_number.ilike(like), Conversation.customer_name.ilike(like), Customer.name.ilike(like),
        ))
    stmt = stmt.order_by(last_msg_at.desc().nullslast(), Conversation.id.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    ids = [r[0].id for r in rows]
    newest_ids = (
        select(func.max(Message.id)).where(Message.conversation_id.in_(ids)).group_by(Message.conversation_id)
    )
    latest = (await db.execute(select(Message).where(Message.id.in_(newest_ids)))).scalars().all()
    last_by_conv = {m.conversation_id: m for m in latest}
    phones = [r[0].phone_number for r in rows]
    leads = dict((await db.execute(
        select(Lead.phone_number, Lead.status).where(Lead.client_id == cid, Lead.phone_number.in_(phones))
    )).all())

    out = []
    for conv, cust_name, unread_n, verify in rows:
        m = last_by_conv.get(conv.id)
        d = channel_sender.message_to_dict(m) if m else None
        out.append(ConversationSummary(
            id=conv.id, customer_name=conv.customer_name or cust_name, phone_number=conv.phone_number,
            channel=conv.channel, lead_status=leads.get(conv.phone_number, "cold"),
            last_message=LastMessageOut(
                text=(m.content or "")[:120], direction=d["direction"], sender_type=d["sender_type"],
                media_type=m.media_type, created_at=_ts(m.created_at),
            ) if m else None,
            unread_count=int(unread_n or 0), payment_to_verify=bool(verify),
            bot_paused=_effective_bot_paused(conv, user.client), ai_enabled=conv.ai_enabled is not False,
            updated_at=_ts(m.created_at if m else conv.updated_at),
        ))
    return out


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: int,
    before_id: Optional[int] = Query(None, description="Load messages older than this id (pagination)."),
    limit: int = Query(_THREAD_PAGE, ge=1, le=300),
    user: User = Depends(_perm),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """
    Return the thread (newest `limit` messages, ascending), the 24h window state,
    inline order events, and the customer-panel data (current + past orders).
    """
    client = user.client
    conv = await _get_conv_or_404(db, client.id, conv_id)

    q = select(Message).where(Message.conversation_id == conv.id)
    if before_id:
        q = q.where(Message.id < before_id)
    rows = list((await db.execute(q.order_by(Message.id.desc()).limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    messages = list(reversed(rows[:limit]))
    total = (await db.execute(select(func.count(Message.id)).where(Message.conversation_id == conv.id))).scalar_one()

    msg_ids = [m.id for m in messages]
    proofs: dict[int, ProofCardOut] = {}
    if msg_ids:
        pr = (await db.execute(
            select(PaymentProof, Order).join(Order, Order.id == PaymentProof.order_id)
            .where(PaymentProof.message_id.in_(msg_ids))
        )).all()
        for p, o in pr:
            proofs[p.message_id] = ProofCardOut(
                proof_id=p.id, order_id=o.id, order_number=o.order_number, amount_expected=o.total_amount,
                status=p.status, order_status=o.status, reviewed_by=p.reviewed_by_name,
                reviewed_at=_ts(p.reviewed_at), reject_reason=p.reject_reason,
            )
    staff_ids = {m.sender_user_id for m in messages if m.sender_user_id}
    names = dict((await db.execute(select(User.id, User.email).where(User.id.in_(staff_ids)))).all()) if staff_ids else {}

    orders = list((await db.execute(
        select(Order).where(Order.client_id == client.id, Order.conversation_id == conv.id)
        .order_by(Order.created_at.desc()).limit(10)
    )).scalars().all())
    events: list[OrderEventOut] = []
    if orders:
        by_id = {o.id: o for o in orders}
        for o in orders:
            events.append(OrderEventOut(order_id=o.id, order_number=o.order_number, status="order_created", at=_ts(o.created_at) or ""))
        audit = (await db.execute(
            select(OrderAuditLog).where(OrderAuditLog.order_id.in_(by_id.keys())).order_by(OrderAuditLog.id)
        )).scalars().all()
        for a in audit:
            events.append(OrderEventOut(
                order_id=a.order_id, order_number=by_id[a.order_id].order_number,
                status=a.to_status or a.action, at=_ts(a.created_at) or "",
            ))
        events.sort(key=lambda e: e.at)

    customer = (await db.execute(
        select(Customer).where(Customer.client_id == client.id, Customer.phone == conv.phone_number)
    )).scalar_one_or_none()
    lead = (await db.execute(
        select(Lead.status).where(Lead.client_id == client.id, Lead.phone_number == conv.phone_number)
    )).scalar_one_or_none()
    open_states = ("new", "pending_payment", "payment_submitted")
    current = next((o for o in orders if o.status in open_states), None)
    resume_at = conversation_control.auto_resume_at(conv, client)

    return ConversationDetail(
        id=conv.id, phone_number=conv.phone_number, channel=conv.channel,
        customer_name=conv.customer_name or (customer.name if customer else None),
        ai_enabled=conv.ai_enabled is not False, bot_paused=_effective_bot_paused(conv, client),
        bot_pause_source=conv.bot_pause_source, auto_resume_at=_ts(resume_at),
        taken_over_at=_ts(conv.taken_over_at), taken_over_note=conv.taken_over_note,
        lead_status=lead or "cold", message_count=int(total),
        created_at=_ts(conv.created_at) or "", updated_at=_ts(conv.updated_at),
        window=await _window(db, conv), opted_out=bool(customer and customer.opted_out),
        messages=[_msg_out(m, conv.channel, proofs, names) for m in messages], has_more=has_more,
        events=events, current_order=_order_brief(current) if current else None,
        orders=[_order_brief(o) for o in orders],
    )


@router.post("/{conv_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(conv_id: int, user: User = Depends(_perm), db: AsyncSession = Depends(get_db)) -> None:
    """Mark everything currently in the thread as read (clears the unread badge)."""
    conv = await _get_conv_or_404(db, user.client_id, conv_id)
    conv.last_read_at = datetime.now(timezone.utc)
    await db.commit()


@router.post("/{conv_id}/media")
async def upload_media(
    conv_id: int, file: UploadFile = File(...),
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Upload an image to attach to an outbound message.

    Returns {media_url, media_type}; pass media_url to POST /conversations/{id}/messages.
    JPEG/PNG/WebP up to 5 MB (WhatsApp's limit).
    """
    await _get_conv_or_404(db, user.client_id, conv_id)
    data = await file.read()
    if len(data) > media_service.MAX_OUTBOUND_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "MEDIA_TOO_LARGE", "message": "Image must be 5 MB or smaller."})
    ctype = media_service.sniff_content_type(data)
    if ctype not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "UNSUPPORTED_MEDIA", "message": "Only JPEG, PNG or WebP images can be sent."})
    url = await media_service.store_media(user.client_id, data, ctype, folder="chat-out")
    return {"media_url": url, "media_type": "image"}


async def _send_human(
    db: AsyncSession, user: User, conv: Conversation, body: SendRequest
) -> SendResult:
    """Shared implementation of the human-send endpoints (see send_message)."""
    client = user.client

    # Idempotent retry: the same client_msg_id never sends twice.
    dedupe_id = f"dash:{conv.id}:{body.client_msg_id}" if body.client_msg_id else None
    if dedupe_id:
        prior = (await db.execute(
            select(Message).where(Message.conversation_id == conv.id, Message.wamid == dedupe_id)
        )).scalar_one_or_none()
        if prior is not None:
            return SendResult(
                message=_msg_out(prior, conv.channel, {}, {prior.sender_user_id: user.email} if prior.sender_user_id else {}),
                bot_paused=_effective_bot_paused(conv, client),
                auto_resume_at=_ts(conversation_control.auto_resume_at(conv, client)),
            )

    if conv.is_sandbox:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "SANDBOX", "message": "Sandbox conversations can't send real messages."})

    if body.template is not None:
        outcome = await _send_template(db, user, conv, body)
    else:
        code = await channel_sender.precheck(db, client, conv, MessageKind.MANUAL_AGENT)
        if code == "WINDOW_CLOSED":
            raise HTTPException(status.HTTP_409_CONFLICT, detail={
                "code": "WINDOW_CLOSED",
                "message": "The 24-hour reply window is closed. Send an approved template instead.",
                "templates": [t.model_dump() for t in await _approved_templates(db, client.id)],
            })
        if code:
            raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": code, "message": f"Message not sent: {code.lower().replace('_', ' ')}."})
        image_url = media_service.absolute_url(body.media_url) if body.media_url else None
        outcome = await channel_sender.send_to_conversation(
            db, client, conv,
            text=(body.caption if image_url else body.text),
            image_url=image_url, persisted_media_url=body.media_url,
            kind=MessageKind.MANUAL_AGENT, sender_type="human", sender_user_id=user.id,
        )

    if not outcome.sent:
        code = outcome.reason or "SEND_FAILED"
        http = status.HTTP_502_BAD_GATEWAY if code == "SEND_FAILED" else status.HTTP_409_CONFLICT
        raise HTTPException(http, detail={"code": code, "message": f"Message not sent ({code})."})

    msg = outcome.message
    if dedupe_id:
        msg.wamid = dedupe_id
        await db.commit()

    # Human takeover: a dashboard send pauses the bot; it auto-resumes after idle.
    await conversation_control.pause_bot(db, client, conv, source=conversation_control.SOURCE_HUMAN_SEND)
    return SendResult(
        message=_msg_out(msg, conv.channel, {}, {user.id: user.email}),
        bot_paused=True,
        auto_resume_at=_ts(conversation_control.auto_resume_at(conv, client)),
    )


async def _send_template(
    db: AsyncSession, user: User, conv: Conversation, body: SendRequest
) -> channel_sender.SendOutcome:
    """Send an approved WhatsApp template (usable inside or outside the window)."""
    client = user.client
    if conv.channel != "whatsapp":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "TEMPLATES_WHATSAPP_ONLY", "message": "Templates are only available on WhatsApp."})
    tpl = (await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.client_id == client.id, MessageTemplate.name == body.template.name,
            MessageTemplate.language == body.template.language, MessageTemplate.status == "approved",
        )
    )).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "TEMPLATE_NOT_APPROVED", "message": "That template isn't approved for this business."})
    try:
        result = await outbound.send_template(
            conv.phone_number, tpl.name, tpl.language, body.template.variables,
            kind=MessageKind.UTILITY_TEMPLATE, db=db, client_id=client.id, conversation_id=conv.id,
            phone_number_id=client.whatsapp_phone_number_id, access_token=client.whatsapp_access_token,
        )
    except Exception as exc:
        logger.error("Template send failed conv=%s: %s", conv.id, exc)
        return channel_sender.SendOutcome(False, "SEND_FAILED")
    if result is None:
        return channel_sender.SendOutcome(False, "SUPPRESSED")
    text = tpl.body
    for i, v in enumerate(body.template.variables, start=1):
        text = text.replace(f"{{{{{i}}}}}", v)
    from app.services import conversation_service, realtime_service

    msg = await conversation_service.save_message(
        db, conv.id, "assistant", text or f"[template: {tpl.name}]",
        channel=conv.channel, sender_type="human", sender_user_id=user.id,
    )
    realtime_service.publish(client.id, "new_message", {"conversation_id": conv.id, "message": channel_sender.message_to_dict(msg)})
    return channel_sender.SendOutcome(True, None, msg)


@router.post("/{conv_id}/messages", response_model=SendResult, status_code=status.HTTP_201_CREATED)
async def send_message(
    conv_id: int, body: SendRequest,
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> SendResult:
    """
    Send a message to the customer as a human (dashboard).

    Inside the 24h window: free-form text or an image. Outside it: 409
    WINDOW_CLOSED with the list of approved utility templates — resend with
    `template`. Opted-out/blocked customers get 409 OPTED_OUT/BLOCKED.
    A successful send pauses the bot (auto-resumes after the idle period).
    """
    conv = await _get_conv_or_404(db, user.client_id, conv_id)
    return await _send_human(db, user, conv, body)


@router.post("/{conv_id}/send-message", response_model=SendResult, status_code=status.HTTP_201_CREATED, deprecated=True)
async def send_message_legacy(
    conv_id: int, body: LegacySendRequest,
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> SendResult:
    """Deprecated alias of POST /conversations/{id}/messages (text only). It now really sends."""
    conv = await _get_conv_or_404(db, user.client_id, conv_id)
    return await _send_human(db, user, conv, SendRequest(text=body.message))


@router.patch("/{conv_id}/takeover", response_model=ConversationDetail)
async def takeover_conversation(
    conv_id: int, body: TakeoverRequest | None = None,
    user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """
    Pause the bot for this conversation (manual pause — never auto-resumes).

    The webhook keeps saving inbound messages but generates no AI reply.
    Payment approve/reject templates still go out.
    """
    conv = await _get_conv_or_404(db, user.client_id, conv_id)
    await conversation_control.pause_bot(
        db, user.client, conv, source=conversation_control.SOURCE_MANUAL, note=body.note if body else None,
    )
    return await get_conversation(conv_id, None, _THREAD_PAGE, user, db)


@router.patch("/{conv_id}/resume", response_model=ConversationDetail)
async def resume_conversation(
    conv_id: int, user: User = Depends(_perm), db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """Resume the bot: the next inbound message is answered by the AI again."""
    conv = await _get_conv_or_404(db, user.client_id, conv_id)
    await conversation_control.resume_bot(db, user.client, conv)
    return await get_conversation(conv_id, None, _THREAD_PAGE, user, db)
