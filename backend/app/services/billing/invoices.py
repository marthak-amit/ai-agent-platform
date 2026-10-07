"""
GST tax invoices for SellerTalk24 plan purchases.

  financial_year(dt)             Indian FY ("2026-27") of an instant, judged in IST (FY runs 1 Apr - 31 Mar).
  allocate_invoice_number(..)    next gapless number "ST24/2026-27/0001" from a row-locked (mode, FY) counter.
                                 Test/mock-mode numbers carry a "TEST-" prefix ("TEST-ST24/2026-27/0001") and have
                                 their own counter, so test payments never advance the live series.
  create_invoice(..)             snapshot a PAID payment order into an immutable `invoices` row (idempotent).
  ensure_invoice(..)             create-if-missing for one order (lazy / admin backfill path).
  backfill_missing_invoices(..)  invoices for paid orders that predate this feature, in payment order.

Why a locked counter and not a Postgres SEQUENCE: sequences skip numbers when a transaction rolls back, and Indian
GST rules expect a consecutive invoice series. The counter row is updated with one atomic
`INSERT .. ON CONFLICT DO UPDATE .. RETURNING`, which holds that row's lock until the caller's transaction ends —
concurrent allocations queue up, and a rolled-back allocation rolls its number back too. Callers should therefore
allocate as late as possible in their transaction and commit soon after.

Amounts are copied from the PaymentOrder (what Razorpay actually charged), never recomputed: the invoice must
equal the payment. The CGST/SGST-vs-IGST split was decided at checkout from the buyer's GSTIN state vs the seller's
state (subscriptions.is_intra_state); an order whose split no longer matches the profile logs a warning.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingPlan,
    ClientSubscription,
    Invoice,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
)
from app.services.billing.subscriptions import is_intra_state, now_utc

logger = logging.getLogger(__name__)

#: SAC for "Online content / software-as-a-service type of services" used on SellerTalk24 invoices.
SAC_CODE = "998314"
IST = timezone(timedelta(hours=5, minutes=30))

#: GSTIN: 2-digit state code, 10-char PAN, entity number, "Z", checksum.
GSTIN_RE = re.compile(r"^(\d{2})[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

#: GST state / UT codes (first two digits of a GSTIN) -> name, for "place of supply".
STATE_NAMES: dict[str, str] = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}


def normalise_gstin(value: str | None) -> str | None:
    """Upper-case/strip a GSTIN; None for blank. Does not validate (see is_valid_gstin)."""
    cleaned = (value or "").strip().upper()
    return cleaned or None


def is_valid_gstin(value: str | None) -> bool:
    """True for a well-formed GSTIN whose state code is a known GST state/UT code."""
    match = GSTIN_RE.match((value or "").strip().upper())
    return bool(match and match.group(1) in STATE_NAMES)


def state_name(code: str | None) -> str:
    """Human name of a GST state code ("24" -> "Gujarat"); "" when unknown/blank."""
    return STATE_NAMES.get(code or "", "")


def financial_year(at: datetime) -> str:
    """
    Indian financial year of *at* as "YYYY-YY" ("2026-27" for 1 Apr 2026 - 31 Mar 2027).

    Judged in IST, so 31 Mar 20:00 UTC (= 1 Apr 01:30 IST) already belongs to the new year.
    A naive datetime is taken as UTC.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    local = at.astimezone(IST)
    start = local.year if local.month >= 4 else local.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def series_prefix(prefix: str, mode: str) -> str:
    """("ST24", "live") -> "ST24"; ("ST24", "test") -> "TEST-ST24" — test numbers can never be mistaken for live ones."""
    return prefix if mode == "live" else f"TEST-{prefix}"


def format_invoice_number(prefix: str, fy: str, seq: int) -> str:
    """("ST24", "2026-27", 7) -> "ST24/2026-27/0007"."""
    return f"{prefix}/{fy}/{seq:04d}"


async def allocate_invoice_number(
    db: AsyncSession, issued_at: datetime, *, mode: str, prefix: str | None = None
) -> tuple[str, str, int]:
    """
    Take the next number of the (*mode*, *issued_at*'s financial year) series.
    Returns (invoice_number, financial_year, seq); test-mode numbers are prefixed "TEST-".

    Does not commit. The counter row stays locked until the caller's transaction ends (see module docstring).
    """
    fy = financial_year(issued_at)
    seq = await db.scalar(
        text(
            "INSERT INTO invoice_counters (mode, financial_year, last_seq) VALUES (:mode, :fy, 1) "
            "ON CONFLICT (mode, financial_year) DO UPDATE SET last_seq = invoice_counters.last_seq + 1 "
            "RETURNING last_seq"
        ),
        {"mode": mode, "fy": fy},
    )
    number = format_invoice_number(series_prefix(prefix or get_settings().invoice_prefix, mode), fy, int(seq))
    return number, fy, int(seq)


def _description(plan: BillingPlan, order: PaymentOrder) -> str:
    """Line-item text: plan, period and (for upgrades) the credit note."""
    label = f"SellerTalk24 {plan.name} plan — {plan.billing_period_days} days, {plan.conversation_limit} conversations"
    if order.purpose == PaymentPurpose.UPGRADE:
        label += " (upgrade; credit for unused period applied)"
    elif order.purpose == PaymentPurpose.RENEWAL:
        label += " (renewal)"
    return label


async def get_invoice_for_order(db: AsyncSession, payment_order_id: int) -> Invoice | None:
    """The invoice already issued for an order, if any."""
    return (
        await db.execute(select(Invoice).where(Invoice.payment_order_id == payment_order_id))
    ).scalar_one_or_none()


async def create_invoice(
    db: AsyncSession,
    order: PaymentOrder,
    client: Client,
    plan: BillingPlan,
    *,
    subscription_id: int | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Invoice:
    """
    Issue the invoice for a paid *order* (idempotent: returns the existing one if already issued).

    Flushes but does not commit. Raises ValueError if the order is not paid.
    """
    existing = await get_invoice_for_order(db, order.id)
    if existing is not None:
        return existing
    if order.status != PaymentOrderStatus.PAID:
        raise ValueError(f"order {order.id} is {order.status!r}; only paid orders get an invoice")

    settings = settings or get_settings()
    now = now or now_utc()
    issued_at = order.paid_at or now

    gstin = normalise_gstin(client.gst_number)
    intra_charged = order.igst_paise == 0
    if is_intra_state(client, settings.seller_state_code) != intra_charged:
        logger.warning(
            "billing: invoice for order %s — buyer GSTIN state no longer matches the tax split charged at "
            "checkout (igst=%s); invoice follows what was charged",
            order.id, order.igst_paise,
        )
    buyer_state = gstin[:2] if gstin and GSTIN_RE.match(gstin) else None
    if not settings.seller_gstin:
        logger.warning("billing: SELLER_GSTIN is not configured — invoice %s prints without a seller GSTIN", order.id)

    number, fy, seq = await allocate_invoice_number(db, issued_at, mode=order.mode, prefix=settings.invoice_prefix)
    invoice = Invoice(
        invoice_number=number, mode=order.mode, financial_year=fy, seq=seq,
        client_id=client.id, payment_order_id=order.id, subscription_id=subscription_id,
        issued_at=issued_at,
        buyer_name=client.business_name or client.email, buyer_email=client.email,
        buyer_gstin=gstin, buyer_address=(client.business_address or None), buyer_state_code=buyer_state,
        seller_name=settings.seller_legal_name, seller_gstin=settings.seller_gstin or None,
        seller_address=settings.seller_address or None, seller_state_code=settings.seller_state_code,
        sac_code=SAC_CODE, description=_description(plan, order), currency=order.currency,
        gst_rate_bps=settings.gst_rate_bps, intra_state=intra_charged,
        base_paise=order.base_paise, credit_paise=order.credit_paise, taxable_paise=order.taxable_paise,
        cgst_paise=order.cgst_paise, sgst_paise=order.sgst_paise, igst_paise=order.igst_paise,
        total_paise=order.amount_paise, razorpay_payment_id=order.razorpay_payment_id,
    )
    db.add(invoice)
    await db.flush()
    logger.info("billing: issued invoice %s for order %s (client %s)", number, order.id, client.id)
    return invoice


async def ensure_invoice(db: AsyncSession, payment_order_id: int, *, now: datetime | None = None) -> Invoice | None:
    """
    Create the invoice for a paid order that lacks one and commit. Returns None when the order is unknown
    or not paid. Safe to call repeatedly / concurrently (the unique key on payment_order_id is the backstop).
    """
    order = (
        await db.execute(
            select(PaymentOrder).where(PaymentOrder.id == payment_order_id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None or order.status != PaymentOrderStatus.PAID:
        await db.commit()  # release the row lock (commit, not rollback: rollback would expire the caller's objects)
        return None
    client = await db.get(Client, order.client_id)
    plan = await db.get(BillingPlan, order.plan_id)
    sub_id = await db.scalar(select(ClientSubscription.id).where(ClientSubscription.source_payment_id == order.id))
    # A savepoint, so a failure undoes only this attempt (incl. its counter bump) and leaves the caller's session usable.
    async with db.begin_nested():
        invoice = await create_invoice(db, order, client, plan, subscription_id=sub_id, now=now)
    await db.commit()
    return invoice


async def backfill_missing_invoices(db: AsyncSession, *, now: datetime | None = None) -> list[Invoice]:
    """Issue invoices for every paid order without one, oldest payment first (so numbers follow payment order)."""
    ids = list(
        (
            await db.execute(
                select(PaymentOrder.id)
                .outerjoin(Invoice, Invoice.payment_order_id == PaymentOrder.id)
                .where(PaymentOrder.status == PaymentOrderStatus.PAID, Invoice.id.is_(None))
                .order_by(PaymentOrder.paid_at, PaymentOrder.id)
            )
        ).scalars()
    )
    created: list[Invoice] = []
    for order_id in ids:
        invoice = await ensure_invoice(db, order_id, now=now)
        if invoice is not None:
            created.append(invoice)
    return created
