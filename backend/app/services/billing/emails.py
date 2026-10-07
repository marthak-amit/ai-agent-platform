"""
Billing emails: payment success (with the invoice PDF), payment failed, expiry reminders, grace started, expired,
plan cancelled after a refund.

`render_email(kind, ...)` is pure (subject, text, html). The `notify_*` functions load what they need, send via
`email_service.send_email` and NEVER raise — a notification problem must not fail a payment activation, a webhook
or a scheduler tick. Callers invoke them only at the moment the corresponding event is first recorded
(activation that was not already processed; a webhook that newly marks an order failed; a billing alert that was
newly created), which is what keeps each customer from getting duplicates.

Language: emails are English. (The dashboard has en/hi/gu; localising email is a follow-up.)
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import BillingPlan, ClientSubscription, PaymentOrder
from app.services import email_service
from app.services.billing import invoices
from app.services.billing.invoice_pdf import render_invoice_pdf, rupees
from app.services.email_service import EmailAttachment, EmailMessage

logger = logging.getLogger(__name__)

KINDS = ("payment_success", "payment_failed", "expiring", "grace_started", "expired", "refund_revoked")


def _billing_url() -> str:
    """Link to the dashboard billing page."""
    return get_settings().frontend_url.rstrip("/") + "/billing"


def _fmt(dt: datetime | None) -> str:
    """'07 Oct 2026' (UTC date); '—' when missing."""
    return f"{dt:%d %b %Y}" if dt else "—"


def render_email(kind: str, ctx: dict[str, Any]) -> tuple[str, str, str]:
    """
    Subject, plain-text body and HTML body for *kind* with context *ctx*.

    ctx keys by kind — payment_success: name, plan, amount, invoice_number, start, end;
    payment_failed: name, plan, amount, reason; expiring: name, plan, days, end;
    grace_started: name, grace_ends; expired: name; refund_revoked: name, plan, amount, grace_ends.
    """
    name = ctx.get("name") or "there"
    url = _billing_url()
    if kind == "payment_success":
        subject = f"Payment received — your {ctx['plan']} plan is active"
        lines = [
            f"Hi {name},",
            f"Thank you! We received your payment of {ctx['amount']} and your {ctx['plan']} plan is active "
            f"from {ctx['start']} to {ctx['end']}.",
            f"Your GST tax invoice {ctx['invoice_number']} is attached to this email, and you can download it "
            f"any time from Billing in your dashboard." if ctx.get("invoice_number") else
            "Your GST tax invoice will be available in Billing in your dashboard shortly.",
        ]
    elif kind == "payment_failed":
        subject = "Your SellerTalk24 payment didn't go through"
        reason = f" Reason: {ctx['reason']}." if ctx.get("reason") else ""
        lines = [
            f"Hi {name},",
            f"Your payment of {ctx['amount']} for the {ctx['plan']} plan could not be completed.{reason} "
            "If any money was deducted from your account, your bank returns it automatically, usually within "
            "5–7 working days.",
            "You can try again from your dashboard whenever you're ready.",
        ]
    elif kind == "expiring":
        days = int(ctx["days"])
        subject = f"Your {ctx['plan']} plan expires in {days} day{'s' if days != 1 else ''}"
        lines = [
            f"Hi {name},",
            f"Your {ctx['plan']} plan ends on {ctx['end']}. Plans don't renew automatically — renew now so your "
            "AI sales agent keeps answering customers without a break.",
        ]
    elif kind == "grace_started":
        subject = "Your SellerTalk24 plan has ended — grace period started"
        lines = [
            f"Hi {name},",
            f"You don't have an active plan right now. Your AI sales agent keeps working until {ctx['grace_ends']}. "
            "Subscribe before then to avoid an interruption.",
        ]
    elif kind == "refund_revoked":
        subject = f"Your {ctx['plan']} payment was refunded — plan cancelled"
        lines = [
            f"Hi {name},",
            f"Your payment of {ctx['amount']} for the {ctx['plan']} plan was refunded, so the plan has been cancelled. "
            f"Your AI sales agent keeps working until {ctx['grace_ends']}.",
            "A GST credit note has been issued for the refund; contact us if you need a copy. "
            "Subscribe again before the grace period ends to avoid an interruption.",
        ]
    elif kind == "expired":
        subject = "Your SellerTalk24 plan has expired"
        lines = [
            f"Hi {name},",
            "Your plan has expired and the grace period is over. New customers now receive a "
            "'temporarily unavailable' reply; orders already in progress still complete. "
            "Subscribe to switch your AI sales agent back on.",
        ]
    else:
        raise ValueError(f"unknown email kind {kind!r}")

    cta = "Open Billing" if kind != "payment_success" else "View your billing page"
    text = "\n\n".join(lines + [f"{cta}: {url}", "— The SellerTalk24 team"])
    paragraphs = "".join(f"<p style=\"margin:0 0 14px\">{escape(line)}</p>" for line in lines)
    html = (
        "<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1A2E44;max-width:560px\">"
        f"{paragraphs}"
        f"<p><a href=\"{escape(url)}\" style=\"background:#0F8B4C;color:#fff;padding:10px 18px;"
        f"border-radius:8px;text-decoration:none;display:inline-block\">{escape(cta)}</a></p>"
        "<p style=\"color:#6B7280\">— The SellerTalk24 team</p></div>"
    )
    return subject, text, html


async def _send(client: Client, kind: str, ctx: dict[str, Any], attachments: list[EmailAttachment] | None = None) -> bool:
    """Render and send one email to the client's account address."""
    ctx = {"name": client.business_name, **ctx}
    subject, text, html = render_email(kind, ctx)
    ok = await email_service.send_email(
        EmailMessage(to=client.email, subject=subject, text=text, html=html, attachments=attachments or [])
    )
    logger.info("billing email %s to client %s: %s", kind, client.id, "sent" if ok else "NOT sent")
    return ok


async def notify_payment_success(db: AsyncSession, payment_order_id: int) -> bool:
    """Payment confirmation + invoice PDF for a paid order. Never raises."""
    try:
        order = await db.get(PaymentOrder, payment_order_id)
        if order is None:
            return False
        client = await db.get(Client, order.client_id)
        plan = await db.get(BillingPlan, order.plan_id)
        sub = (
            await db.execute(select(ClientSubscription).where(ClientSubscription.source_payment_id == order.id))
        ).scalar_one_or_none()
        invoice = await invoices.get_invoice_for_order(db, order.id)
        if invoice is None:  # activation could not issue it (logged there) — try again, but never lose the email
            try:
                invoice = await invoices.ensure_invoice(db, order.id)
            except Exception:
                # No rollback here: it would expire the caller's ORM objects. ensure_invoice already undid its own
                # savepoint; committing just releases the order row lock it took.
                await db.commit()
                logger.exception("billing: could not issue the invoice for order %s — emailing without it", order.id)
        attachments: list[EmailAttachment] = []
        if invoice is not None:
            try:
                attachments.append(EmailAttachment(
                    filename=invoice.invoice_number.replace("/", "-") + ".pdf", content=render_invoice_pdf(invoice),
                ))
            except Exception:
                logger.exception("billing: invoice PDF render failed for %s — sending the email without it", invoice.invoice_number)
        return await _send(client, "payment_success", {
            "plan": plan.name, "amount": rupees(order.amount_paise),
            "invoice_number": invoice.invoice_number if invoice else None,
            "start": _fmt(sub.current_period_start if sub else None), "end": _fmt(sub.current_period_end if sub else None),
        }, attachments)
    except Exception:
        logger.exception("billing: payment-success email failed for order %s", payment_order_id)
        return False


async def notify_payment_failed(db: AsyncSession, payment_order_id: int) -> bool:
    """'Your payment didn't go through' for an order that was just marked failed. Never raises."""
    try:
        order = await db.get(PaymentOrder, payment_order_id)
        if order is None:
            return False
        client = await db.get(Client, order.client_id)
        plan = await db.get(BillingPlan, order.plan_id)
        return await _send(client, "payment_failed", {
            "plan": plan.name, "amount": rupees(order.amount_paise), "reason": order.failure_reason,
        })
    except Exception:
        logger.exception("billing: payment-failed email failed for order %s", payment_order_id)
        return False


_ALERT_TO_EMAIL = {
    "expiring_3d": "expiring", "expiring_1d": "expiring", "grace_started": "grace_started", "expired": "expired",
    "refund_revoked": "refund_revoked",
}


async def notify_alert(db: AsyncSession, client_id: int, alert_kind: str, params: dict[str, Any]) -> bool:
    """
    Email for a newly raised billing alert (expiry reminders, grace started, expired). Usage-threshold alerts
    are dashboard-only and are ignored here. Never raises.
    """
    kind = _ALERT_TO_EMAIL.get(alert_kind)
    if kind is None:
        return False
    try:
        client = await db.get(Client, client_id)
        if client is None:
            return False
        return await _send(client, kind, dict(params))
    except Exception:
        logger.exception("billing: %s email failed for client %s", alert_kind, client_id)
        return False
