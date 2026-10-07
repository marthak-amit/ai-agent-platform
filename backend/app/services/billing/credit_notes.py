"""
GST credit notes for Razorpay refunds.

  split_refund(order, amount)    refunded value -> (taxable, cgst, sgst, igst) in the order's own proportions.
  allocate_credit_note_number()  next gapless "ST24-CN/2026-27/0001" (test: "TEST-ST24-CN/...") from a (mode, FY) counter.
  issue_credit_note(..)          one credit note per Razorpay refund id (idempotent on that id).
  refunded_total(..)             sum of the credit notes already issued against an order.

Same locking story as invoices: the counter row is bumped with one `INSERT .. ON CONFLICT DO UPDATE .. RETURNING`,
so a rolled-back transaction also rolls its number back. Callers run issue_credit_note inside the transaction that
records the refund. Amounts are paise.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sellertalk24_billing import CreditNote, Invoice, PaymentOrder
from app.services.billing.invoices import financial_year, format_invoice_number, series_prefix
from app.services.billing.subscriptions import now_utc

logger = logging.getLogger(__name__)


def split_refund(order: PaymentOrder, amount_paise: int) -> tuple[int, int, int, int]:
    """
    Split *amount_paise* of a refund into (taxable, cgst, sgst, igst), in the proportions of the original order.

    A full refund (amount == order total) returns the order's own figures exactly; a partial refund takes
    the GST share proportionally and gives any rounding remainder to the taxable part, so the pieces always sum to
    the refunded amount.
    """
    total = order.amount_paise
    if total <= 0 or amount_paise <= 0:
        return 0, 0, 0, 0
    if amount_paise >= total:
        return order.taxable_paise, order.cgst_paise, order.sgst_paise, order.igst_paise
    cgst = round(amount_paise * order.cgst_paise / total)
    sgst = round(amount_paise * order.sgst_paise / total)
    igst = round(amount_paise * order.igst_paise / total)
    return amount_paise - cgst - sgst - igst, cgst, sgst, igst


async def allocate_credit_note_number(
    db: AsyncSession, issued_at: datetime, *, mode: str, prefix: str | None = None
) -> tuple[str, str, int]:
    """Next (credit_note_number, financial_year, seq) of the (mode, FY) series. Does not commit."""
    fy = financial_year(issued_at)
    seq = await db.scalar(
        text(
            "INSERT INTO credit_note_counters (mode, financial_year, last_seq) VALUES (:mode, :fy, 1) "
            "ON CONFLICT (mode, financial_year) DO UPDATE SET last_seq = credit_note_counters.last_seq + 1 "
            "RETURNING last_seq"
        ),
        {"mode": mode, "fy": fy},
    )
    base = f"{prefix or get_settings().invoice_prefix}-CN"
    return format_invoice_number(series_prefix(base, mode), fy, int(seq)), fy, int(seq)


async def get_credit_note_for_refund(db: AsyncSession, razorpay_refund_id: str) -> CreditNote | None:
    """The credit note already issued for this Razorpay refund id, if any."""
    return (
        await db.execute(select(CreditNote).where(CreditNote.razorpay_refund_id == razorpay_refund_id))
    ).scalar_one_or_none()


async def refunded_total(db: AsyncSession, payment_order_id: int) -> int:
    """Paise already refunded (= credit notes issued) against an order."""
    return int(await db.scalar(
        select(func.coalesce(func.sum(CreditNote.total_paise), 0)).where(CreditNote.payment_order_id == payment_order_id)
    ) or 0)


async def issue_credit_note(
    db: AsyncSession, order: PaymentOrder, *, razorpay_refund_id: str, amount_paise: int, kind: str,
    reason: str = "", now: datetime | None = None,
) -> tuple[CreditNote, bool]:
    """
    Issue the credit note for one refund. Returns (credit_note, created); created=False when this refund id already
    has one (a redelivered event) — nothing is allocated or changed then. Flushes, does not commit.
    """
    existing = await get_credit_note_for_refund(db, razorpay_refund_id)
    if existing is not None:
        return existing, False
    now = now or now_utc()
    taxable, cgst, sgst, igst = split_refund(order, amount_paise)
    invoice_id = await db.scalar(select(Invoice.id).where(Invoice.payment_order_id == order.id))
    number, fy, seq = await allocate_credit_note_number(db, now, mode=order.mode)
    note = CreditNote(
        mode=order.mode, credit_note_number=number, financial_year=fy, seq=seq, client_id=order.client_id,
        payment_order_id=order.id, invoice_id=invoice_id, razorpay_refund_id=razorpay_refund_id,
        refund_kind=kind, reason=reason, issued_at=now, currency=order.currency,
        taxable_paise=taxable, cgst_paise=cgst, sgst_paise=sgst, igst_paise=igst, total_paise=amount_paise,
    )
    db.add(note)
    await db.flush()
    logger.info("billing: issued credit note %s (%s refund of %s paise) for order %s", number, kind, amount_paise, order.id)
    return note, True
