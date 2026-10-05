"""
Manual UPI payment verification — the engine behind "customer pays the seller's
UPI directly, sends a screenshot, seller approves/rejects from the dashboard".

We never collect money. The LLM only *understands* ("paid" intent); everything
that changes state or talks to the customer here is deterministic: state
transitions go through order_state_machine, replies are EN/HI/GU templates.

Concurrency/idempotency model: every state change takes a row lock on the
order (SELECT … FOR UPDATE) and re-reads its status, so a double-clicked
Approve — or an approve racing a reject/expiry — serialises; the loser sees the
new status, performs no stock change and sends no message (`changed=False`).
Approve deducts stock, consumes the reservation, flips the order to paid and
writes the audit row in ONE transaction; customer/owner notifications happen
after the commit and can never roll the payment back.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.order import Order
from app.models.order_audit_log import OrderAuditLog
from app.models.payment_proof import (
    PROOF_APPROVED,
    PROOF_PENDING,
    PROOF_REJECTED,
    PaymentProof,
)
from app.services import (
    channel_sender,
    media_service,
    order_service,
    realtime_service,
    stock_reservation_service,
)
from app.services.language_templates import format_price, get_template
from app.services.order_state_machine import (
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_PAYMENT_SUBMITTED,
    ORDER_PENDING_PAYMENT,
    PAYMENT_OPEN_STATES,
    InvalidOrderTransition,
    assert_order_transition,
)
from app.services.send_gate import MessageKind

logger = logging.getLogger(__name__)

#: name@handle — handle is the PSP suffix (okaxis, ybl, upi, paytm, …).
UPI_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$")

#: Minimum gap between "verification in progress" acks for extra screenshots.
PROOF_ACK_THROTTLE = timedelta(minutes=10)

#: Waiting longer than this is flagged red in the dashboard.
OVERDUE_AFTER = timedelta(hours=2)

# ─── errors ───────────────────────────────────────────────────────────────────


class OrderNotFound(Exception):
    """The order does not exist for this client."""


# ─── small pure helpers ───────────────────────────────────────────────────────


def is_valid_upi_id(value: str) -> bool:
    """Return True when value looks like name@handle."""
    return bool(UPI_ID_RE.fullmatch(value or ""))


def build_upi_uri(upi_id: str, payee_name: str | None, amount: float, order_id: int) -> str:
    """
    Build the upi://pay deep link encoded into the QR.

    Format: upi://pay?pa={upi_id}&pn={payee}&am={amount}&cu=INR&tn=Order{order_id}
    """
    return (
        f"upi://pay?pa={quote(upi_id, safe='@')}"
        f"&pn={quote(payee_name or '', safe='')}"
        f"&am={amount:.2f}&cu=INR&tn=Order{order_id}"
    )


def make_qr_png(data: str) -> bytes:
    """Render `data` as a PNG QR code (pure-python, no image-library dependency)."""
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


_PAID_RE = re.compile(
    r"(?:\b(?:paid|payment\s*(?:done|made|complete|completed|kar\s*diya|kiya|ho\s*gaya|kari\s*didhu|thai\s*gayu)|"
    r"(?:i\s*(?:have|'ve|ve)\s*)paid|done|sent|transferred|kar\s*diya|ho\s*gaya|bhej\s*diya|"
    r"pay\s*kar\s*diya|kari\s*didhu|thai\s*gayu|moklyu|mokli\s*didhu)\b"
    r"|पेमेंट\s*(?:कर\s*दिया|हो\s*गया)|भुगतान\s*कर\s*दिया|पैसे\s*भेज|ચુકવણી\s*કરી|પેમેન્ટ\s*(?:કરી|થઈ))",
    re.IGNORECASE,
)


def is_paid_intent(text: str) -> bool:
    """
    True for a short "I've paid" style message (EN/HI/GU, roman or native script).

    Length-capped so a long sentence that merely contains "done" ("is the
    order done?") is not misread as a payment claim.
    """
    t = (text or "").strip()
    if not t or len(t.split()) > 8:
        return False
    return bool(_PAID_RE.search(t))


def actor_label(user) -> str:
    """Human-readable audit label for a staff user ('system' when None)."""
    return getattr(user, "email", None) or "system"


def _lang(conv: Conversation | None) -> str:
    """Template language bucket for a conversation."""
    return getattr(conv, "last_customer_language", None) or "english"


def render_items(order: Order) -> str:
    """One bullet per line item: '• Name (Color/Size) × qty = ₹x'."""
    items = getattr(order, "line_items", None) or []
    lines = []
    if items:
        for li in items:
            variant = "/".join(p for p in (li.variant_color, li.variant_size, li.variant_material) if p)
            label = f"{li.product_name} ({variant})" if variant else li.product_name
            lines.append(f"• {label} × {li.quantity} = {format_price(li.subtotal)}")
    else:
        variant = "/".join(p for p in (order.variant_color, order.variant_size) if p)
        label = f"{order.product_name} ({variant})" if variant else order.product_name
        lines.append(f"• {label} × {order.quantity} = {format_price(order.total_amount)}")
    return "\n".join(lines)


# ─── payment instruction (sent when an order enters pending_payment) ──────────


@dataclass
class PaymentInstruction:
    """Deterministic payment message: text, optional QR image, and the closing line."""

    text: str
    qr_url: str | None
    closing: str


def instruction_text(order: Order, client: Client, lang: str) -> str:
    """Order summary + amount + UPI ID (+ seller's custom note), in the customer's language."""
    payee = getattr(client, "upi_display_name", None)
    extra = getattr(client, "payment_instructions", None)
    return get_template(
        lang, "pay_instruction",
        order_number=order.order_number,
        items=render_items(order),
        amount=format_price(order.total_amount),
        upi_id=client.upi_id,
        payee_line=f"\n{payee}" if payee else "",
        extra=f"\n{extra}" if extra else "",
    )


async def build_payment_instruction(
    db: AsyncSession, order: Order, client: Client, conv: Conversation | None
) -> PaymentInstruction:
    """
    Build the order summary + amount + UPI ID text and the generated UPI QR.

    The QR encodes upi://pay?…&am={total}&tn=Order{id}. If generation or
    upload fails the seller's static QR (Client.upi_qr_url) is used instead;
    if neither exists the closing line is folded into the text so the
    instruction still ends with "send the screenshot".
    """
    lang = _lang(conv)
    payee = getattr(client, "upi_display_name", None)
    text = instruction_text(order, client, lang)
    closing = get_template(lang, "pay_send_screenshot")

    qr_url: str | None = None
    try:
        png = make_qr_png(build_upi_uri(client.upi_id, payee, order.total_amount, order.id))
        qr_url = media_service.absolute_url(
            await media_service.store_media(client.id, png, "image/png", folder="payment-qr")
        )
    except Exception as exc:
        logger.error("UPI QR generation failed for order %s: %s", order.id, exc)
    if qr_url is None and getattr(client, "upi_qr_url", None):
        qr_url = media_service.absolute_url(client.upi_qr_url)

    if qr_url is None:
        text = f"{text}\n\n{closing}"
    return PaymentInstruction(text=text, qr_url=qr_url, closing=closing)


async def raise_missing_upi_alert(db: AsyncSession, client: Client, conv: Conversation | None) -> None:
    """
    A customer reached the payment step but the seller has no UPI ID.

    Stamps Client.payment_setup_alert_at (dashboard banner), pushes a realtime
    event, and pings the owner on WhatsApp — the latter at most once an hour.
    """
    now = datetime.now(timezone.utc)
    previous = client.payment_setup_alert_at
    client.payment_setup_alert_at = now
    await db.commit()
    realtime_service.publish(client.id, "payment_setup_required", {"conversation_id": getattr(conv, "id", None)})
    if previous is not None and (now - previous) < timedelta(hours=1):
        return
    try:
        from app.services import outbound

        if client.phone:
            await outbound.send_owner_text(
                client.phone,
                "⚠️ A customer is ready to pay but you haven't added your UPI ID yet. "
                "Add it in Settings → Payment details so orders can continue.",
                phone_number_id=client.whatsapp_phone_number_id,
                access_token=client.whatsapp_access_token,
            )
    except Exception as exc:
        logger.warning("Missing-UPI owner ping failed for client %s: %s", client.id, exc)


# ─── internals ────────────────────────────────────────────────────────────────


async def _lock_order(db: AsyncSession, client_id: int, order_id: int) -> Order:
    """Fetch the order FOR UPDATE (scoped to the client), refreshing any cached state."""
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.client_id == client_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise OrderNotFound(order_id)
    return order


async def _conversation_for(db: AsyncSession, order: Order) -> Conversation | None:
    """The conversation an order belongs to, if any."""
    if not order.conversation_id:
        return None
    return (
        await db.execute(select(Conversation).where(Conversation.id == order.conversation_id))
    ).scalar_one_or_none()


def _audit(
    db: AsyncSession, order: Order, *, actor_user, actor_name: str, action: str,
    from_status: str | None, to_status: str | None, detail: dict | None = None,
) -> None:
    """Queue an audit-log row in the caller's transaction."""
    db.add(OrderAuditLog(
        client_id=order.client_id, order_id=order.id,
        actor_user_id=getattr(actor_user, "id", None), actor_name=actor_name,
        action=action, from_status=from_status, to_status=to_status, detail=detail,
    ))


async def _pending_proofs(db: AsyncSession, order_id: int) -> list[PaymentProof]:
    """Still-pending proofs of an order, locked for the decision."""
    return list(
        (
            await db.execute(
                select(PaymentProof)
                .where(PaymentProof.order_id == order_id, PaymentProof.status == PROOF_PENDING)
                .with_for_update()
            )
        ).scalars().all()
    )


async def _notify_customer(
    db: AsyncSession, client: Client, conv: Conversation | None, key: str, **kwargs
) -> channel_sender.SendOutcome | None:
    """
    Send a deterministic payment template to the customer.

    Deliberately NOT subject to the bot-pause flag: approve/reject/cancel are
    seller decisions, and the customer must hear about them even while the
    seller is handling the chat by hand. Still subject to opt-out/block/24h.
    """
    if conv is None or getattr(conv, "is_sandbox", False):
        return None
    text = get_template(_lang(conv), key, **kwargs)
    return await channel_sender.send_to_conversation(
        db, client, conv, text=text,
        kind=MessageKind.UTILITY_TEMPLATE, sender_type="system",
    )


async def _reset_conversation_after_terminal(db: AsyncSession, conv: Conversation | None) -> None:
    """Return a payment-stage conversation to a clean greeting state (best-effort)."""
    if conv is None or (conv.current_stage or "greeting") not in ("payment", "completed"):
        return
    try:
        from app.services.order_pipeline import _reset_order_slots_after_completion

        await _reset_order_slots_after_completion(db, conv.id, conv)
    except Exception as exc:
        logger.error("Conversation reset after terminal order failed (conv=%s): %s", conv.id, exc)


# ─── customer submits a screenshot ────────────────────────────────────────────


async def find_open_payment_order(db: AsyncSession, conversation_id: int) -> Order | None:
    """Latest non-COD order of the conversation still in the payment flow."""
    return (
        await db.execute(
            select(Order)
            .where(
                Order.conversation_id == conversation_id,
                Order.status.in_(tuple(PAYMENT_OPEN_STATES)),
                Order.payment_method != "COD",
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass
class ProofSubmission:
    """Outcome of attaching a screenshot to an order."""

    proof: PaymentProof
    transitioned: bool  # True when this proof moved the order pending_payment → payment_submitted


async def submit_proof(
    db: AsyncSession, client: Client, conv: Conversation, order_id: int, *,
    message_id: int | None, media_url: str,
) -> ProofSubmission:
    """
    Attach a customer screenshot to an order.

    pending_payment → payment_submitted on the first proof; extra proofs while
    already payment_submitted just add a row. The state change, proof row and
    audit entry commit together. No vision model is ever run on the image.
    """
    order = await _lock_order(db, client.id, order_id)
    now = datetime.now(timezone.utc)
    transitioned = False
    if order.status == ORDER_PENDING_PAYMENT:
        assert_order_transition(order.status, ORDER_PAYMENT_SUBMITTED)
        _audit(db, order, actor_user=None, actor_name="customer", action="payment_submitted",
               from_status=order.status, to_status=ORDER_PAYMENT_SUBMITTED)
        order.status = ORDER_PAYMENT_SUBMITTED
        order.payment_submitted_at = now
        transitioned = True
    elif order.status != ORDER_PAYMENT_SUBMITTED:
        raise InvalidOrderTransition(order.status, ORDER_PAYMENT_SUBMITTED)

    proof = PaymentProof(
        order_id=order.id, client_id=client.id, conversation_id=conv.id,
        message_id=message_id, media_url=media_url, status=PROOF_PENDING,
    )
    db.add(proof)
    await db.commit()
    await db.refresh(proof)
    realtime_service.publish(client.id, "payment_submitted", {
        "order_id": order.id, "conversation_id": conv.id,
        "proof_id": proof.id, "additional": not transitioned,
    })
    return ProofSubmission(proof=proof, transitioned=transitioned)


def proof_ack_due(conv: Conversation, now: datetime | None = None) -> bool:
    """True when the throttled 'verification in progress' ack may be sent (once per 10 min)."""
    last = conv.last_proof_ack_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - last) >= PROOF_ACK_THROTTLE


# ─── seller decisions ─────────────────────────────────────────────────────────


@dataclass
class DecisionResult:
    """
    Outcome of approve / reject / cancel.

    changed:           False when the order was already in the target state
                       (idempotent replay) — nothing was written or sent.
    customer_notified: True/False when a template was attempted, None when not.
    notify_failure:    send-gate/transport reason when the template wasn't delivered.
    """

    order: Order
    changed: bool
    customer_notified: bool | None = None
    notify_failure: str | None = None


def _apply_outcome(result: DecisionResult, outcome: channel_sender.SendOutcome | None) -> None:
    """Fold a SendOutcome into a DecisionResult."""
    if outcome is None:
        return
    result.customer_notified = outcome.sent
    result.notify_failure = None if outcome.sent else outcome.reason


async def approve(db: AsyncSession, client: Client, order_id: int, user) -> DecisionResult:
    """
    payment_submitted → paid. Idempotent.

    One transaction: lock order → validate edge → approve pending proofs →
    deduct stock + consume reservation → mark paid → audit → commit. Then
    (only if this call made the change) message the customer, ping the owner,
    send the invoice and reset the conversation.

    Raises:
        OrderNotFound, InvalidOrderTransition (e.g. approving a pending_payment
        order that has no screenshot, or a cancelled one).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_PAID:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_PAID)

    now = datetime.now(timezone.utc)
    name = actor_label(user)
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_APPROVED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now

    if not order.stock_deducted:
        await order_service.apply_stock_deduction(db, order)
    order.stock_deducted = True
    from_status = order.status
    order.status = ORDER_PAID
    order.payment_status = "paid"
    order.paid_at = now
    order.confirmed_at = order.confirmed_at or now
    _audit(db, order, actor_user=user, actor_name=name, action="payment_approved",
           from_status=from_status, to_status=ORDER_PAID)
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "approved", "reviewed_by": name,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_confirmed", order_number=order.order_number))
    await order_service.run_post_paid_side_effects(db, order, client)
    await _reset_conversation_after_terminal(db, conv)
    return result


async def reject(
    db: AsyncSession, client: Client, order_id: int, user, reason: str | None = None
) -> DecisionResult:
    """
    payment_submitted → pending_payment. Idempotent.

    Stock stays reserved (the customer may simply re-send a clearer screenshot).
    A repeated reject on an order already back in pending_payment is a no-op.

    Raises:
        OrderNotFound, InvalidOrderTransition (order is paid or cancelled).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_PENDING_PAYMENT:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_PENDING_PAYMENT)

    reason = (reason or "").strip() or None
    now = datetime.now(timezone.utc)
    name = actor_label(user)
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_REJECTED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now
        proof.reject_reason = reason
    from_status = order.status
    order.status = ORDER_PENDING_PAYMENT
    _audit(db, order, actor_user=user, actor_name=name, action="payment_rejected",
           from_status=from_status, to_status=ORDER_PENDING_PAYMENT, detail={"reason": reason})
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "rejected", "reviewed_by": name, "reason": reason,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_rejected", reason_part=f": {reason}" if reason else ""))
    return result


async def cancel(
    db: AsyncSession, client: Client, order_id: int, user, reason: str | None = None,
    *, source: str = "seller",
) -> DecisionResult:
    """
    pending_payment | payment_submitted → cancelled. Idempotent.

    Releases the stock reservation (nothing was deducted) and rejects any
    still-pending proofs. `source` is 'seller' or 'expiry' (audit detail).

    Raises:
        OrderNotFound, InvalidOrderTransition (order is already paid).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_CANCELLED:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_CANCELLED)

    reason = (reason or "").strip() or None
    now = datetime.now(timezone.utc)
    name = actor_label(user)
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_REJECTED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now
        proof.reject_reason = reason or "order cancelled"
    await stock_reservation_service.release_for_order(db, order)
    from_status = order.status
    order.status = ORDER_CANCELLED
    order.cancelled_at = now
    order.cancel_reason = reason
    _audit(db, order, actor_user=user, actor_name=name, action="order_cancelled",
           from_status=from_status, to_status=ORDER_CANCELLED,
           detail={"reason": reason, "source": source})
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "cancelled", "reviewed_by": name, "reason": reason,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_cancelled",
        order_number=order.order_number, reason_part=f" {reason}" if reason else ""))
    await _reset_conversation_after_terminal(db, conv)
    return result


async def cancel_by_customer(db: AsyncSession, order: Order) -> None:
    """
    The customer cancelled an unpaid order in chat (pipeline cancel paths).

    Mirrors cancel() for the pending_payment → cancelled edge — status, stamp,
    reservation release and audit row commit together — but sends no template
    (the bot's own cancel reply already told the customer) and does not touch
    orders that already carry a screenshot (payment_submitted): those need the
    seller to decide, since money may have moved.
    """
    assert_order_transition(order.status, ORDER_CANCELLED)
    order.status = ORDER_CANCELLED
    order.cancelled_at = datetime.now(timezone.utc)
    order.cancel_reason = "cancelled by customer"
    await stock_reservation_service.release_for_order(db, order)
    _audit(db, order, actor_user=None, actor_name="customer", action="order_cancelled",
           from_status=ORDER_PENDING_PAYMENT, to_status=ORDER_CANCELLED, detail={"source": "customer"})
    await db.commit()
    realtime_service.publish(order.client_id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "cancelled", "reviewed_by": "customer", "reason": order.cancel_reason,
    })


async def expire_stale_orders(db: AsyncSession, now: datetime | None = None) -> int:
    """
    Cancel pending_payment orders unpaid past their client's payment_expiry_hours.

    payment_submitted orders are NEVER auto-expired: a screenshot is waiting on
    the seller, and cancelling under a customer who already paid would be
    wrong — those are surfaced as overdue in the dashboard instead.

    Returns:
        Number of orders cancelled.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Order.id, Order.client_id, Order.created_at, Client.payment_expiry_hours)
            .join(Client, Client.id == Order.client_id)
            .where(Order.status == ORDER_PENDING_PAYMENT, Order.payment_method != "COD")
        )
    ).all()
    cancelled = 0
    for order_id, client_id, created_at, expiry_hours in rows:
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if now - created_at < timedelta(hours=expiry_hours or 24):
            continue
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        try:
            res = await cancel(db, client, order_id, None, "payment not received in time", source="expiry")
            cancelled += int(res.changed)
        except (InvalidOrderTransition, OrderNotFound):
            await db.rollback()
        except Exception as exc:
            await db.rollback()
            logger.error("expire_stale_orders: order %s failed: %s", order_id, exc)
    return cancelled


# ─── dashboard queries ────────────────────────────────────────────────────────


@dataclass
class PendingPayment:
    """One order with its proofs, shaped for the verification page."""

    order: Order
    channel: str | None
    proofs: list[PaymentProof] = field(default_factory=list)
    waiting_since: datetime | None = None


async def count_pending(db: AsyncSession, client_id: int) -> int:
    """Orders currently waiting for seller verification (sidebar badge)."""
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Order).where(
                    Order.client_id == client_id, Order.status == ORDER_PAYMENT_SUBMITTED
                )
            )
        ).scalar_one()
    )


async def list_payments(
    db: AsyncSession, client_id: int, *, status: str = PROOF_PENDING,
    date_from: date | None = None, date_to: date | None = None,
    search: str | None = None, limit: int = 50, offset: int = 0,
) -> list[PendingPayment]:
    """
    Orders with payment proofs in `status`, newest-waiting first (oldest first for pending).

    status 'pending'  → orders currently payment_submitted (dated by submission time);
    'approved' / 'rejected' → orders having a proof with that decision (dated by review time).
    """
    pending = status == PROOF_PENDING
    date_col = PaymentProof.created_at if pending else PaymentProof.reviewed_at
    stmt = (
        select(Order)
        .join(PaymentProof, PaymentProof.order_id == Order.id)
        .where(Order.client_id == client_id, PaymentProof.status == status)
    )
    if pending:
        stmt = stmt.where(Order.status == ORDER_PAYMENT_SUBMITTED)
    if date_from:
        stmt = stmt.where(date_col >= datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(date_col < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Order.order_number.ilike(like), Order.customer_name.ilike(like),
            Order.customer_phone.ilike(like),
        ))
    order_by = func.min(date_col).asc() if pending else func.max(date_col).desc()
    stmt = stmt.group_by(Order.id).order_by(order_by).limit(limit).offset(offset)
    orders = list((await db.execute(stmt)).scalars().all())
    if not orders:
        return []

    ids = [o.id for o in orders]
    proofs = (
        await db.execute(
            select(PaymentProof)
            .where(PaymentProof.order_id.in_(ids), PaymentProof.status == status)
            .order_by(PaymentProof.created_at.asc())
        )
    ).scalars().all()
    by_order: dict[int, list[PaymentProof]] = {}
    for p in proofs:
        by_order.setdefault(p.order_id, []).append(p)

    conv_ids = [o.conversation_id for o in orders if o.conversation_id]
    channels = dict(
        (await db.execute(
            select(Conversation.id, Conversation.channel).where(Conversation.id.in_(conv_ids))
        )).all()
    ) if conv_ids else {}

    out = []
    for o in orders:
        plist = by_order.get(o.id, [])
        out.append(PendingPayment(
            order=o, channel=channels.get(o.conversation_id), proofs=plist,
            waiting_since=(plist[0].created_at if plist else o.payment_submitted_at),
        ))
    return out
