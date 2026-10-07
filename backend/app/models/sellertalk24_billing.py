"""
ORM models for SellerTalk24 prepaid billing (Razorpay Orders API).

Five tables: the sellable plans (billing_plans), one row per purchased period
(client_subscriptions), one row per Razorpay checkout (payment_orders), the
webhook audit/idempotency log (payment_events) and the per-window conversation
counter (conversation_usage_log).

Conventions:
  * Every amount is an integer number of paise — never a float.
  * "Enum" columns are plain strings guarded by CHECK constraints (the codebase
    uses no native Postgres enum types; they are painful to extend). The allowed
    values live in the *Status / PaymentPurpose / *Channel classes below.
  * The legacy `plans` table / Client.plan_slug are untouched; these tables are
    additive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SubscriptionStatus:
    """Allowed values of client_subscriptions.status."""

    PENDING = "pending"          # created/stacked for a future period, not yet running
    ACTIVE = "active"            # the one running period (max one per client)
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"    # replaced mid-cycle by an upgrade
    REVOKED = "revoked"          # cut short by a full refund or an admin; grace counts from revoked_at
    ALL = (PENDING, ACTIVE, EXPIRED, CANCELLED, SUPERSEDED, REVOKED)


class PaymentOrderStatus:
    """Allowed values of payment_orders.status."""

    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    ALL = (CREATED, ATTEMPTED, PAID, FAILED, REFUNDED)


class PaymentPurpose:
    """Allowed values of payment_orders.purpose."""

    NEW = "new"
    RENEWAL = "renewal"
    UPGRADE = "upgrade"
    ALL = (NEW, RENEWAL, UPGRADE)


class BillingMode:
    """Allowed values of the `mode` column (payment_orders, invoices, credit notes, counters)."""

    TEST = "test"    # mock gateway or Razorpay test keys — numbers carry the "TEST-" prefix
    LIVE = "live"    # Razorpay live keys
    ALL = (TEST, LIVE)


class UsageChannel:
    """Allowed values of conversation_usage_log.channel."""

    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"
    WEBSITE = "website"
    ALL = (WHATSAPP, INSTAGRAM, WEBSITE)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Render a SQL `col IN ('a','b')` check expression from constant values."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class BillingPlan(Base):
    """
    A sellable SellerTalk24 plan (e.g. code "growth_5000").

    price_paise is the GST-EXCLUSIVE price for one billing_period_days period.
    `features` is a JSON object of feature flags, e.g. {"whatsapp": true, "instagram": false}.
    """

    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_sellertalk24_billing_plans_code"),
        CheckConstraint("price_paise >= 0", name="ck_sellertalk24_billing_plans_price_nonneg"),
        CheckConstraint("conversation_limit > 0", name="ck_sellertalk24_billing_plans_limit_pos"),
        CheckConstraint("billing_period_days > 0", name="ck_sellertalk24_billing_plans_period_pos"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    conversation_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    billing_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    features: Mapped[Any] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        """Short debug form: id, code and price."""
        return f"<BillingPlan id={self.id} code={self.code!r} price_paise={self.price_paise}>"


class ClientSubscription(Base):
    """
    One purchased (or stacked) billing period for a client.

    conversation_limit is a snapshot of the plan's limit at purchase time so a
    later plan edit never changes an already-paid period. credited_paise is the
    pro-rata upgrade credit that was applied to the payment that created this row.
    A partial unique index guarantees at most one status='active' row per client.
    """

    __tablename__ = "client_subscriptions"
    __table_args__ = (
        CheckConstraint(
            _in_list("status", SubscriptionStatus.ALL),
            name="ck_sellertalk24_client_subscriptions_status",
        ),
        CheckConstraint(
            "current_period_end > current_period_start",
            name="ck_sellertalk24_client_subscriptions_period_order",
        ),
        CheckConstraint(
            "conversations_used >= 0 AND credited_paise >= 0",
            name="ck_sellertalk24_client_subscriptions_nonneg",
        ),
        Index(
            "uq_sellertalk24_client_subscriptions_one_active",
            "client_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_sellertalk24_client_subscriptions_client_status", "client_id", "status"),
        Index("ix_sellertalk24_client_subscriptions_status_period_end", "status", "current_period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("billing_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=SubscriptionStatus.PENDING,
        server_default=SubscriptionStatus.PENDING,
    )
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conversation_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    conversations_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    credited_paise: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Soft-cap flag: set when conversations_used goes ABOVE conversation_limit. The bot keeps working;
    # the dashboard shows an upgrade banner.
    over_limit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source_payment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("payment_orders.id"), nullable=True
    )
    # Set when status becomes 'revoked'; the grace period is measured from here (not from current_period_end).
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        """Short debug form: id, client, status and used/limit."""
        return (
            f"<ClientSubscription id={self.id} client_id={self.client_id} "
            f"status={self.status!r} used={self.conversations_used}/{self.conversation_limit}>"
        )


class PaymentOrder(Base):
    """
    One Razorpay Orders-API checkout for a plan purchase.

    Amounts (paise): base_paise is the plan price; credit_paise the upgrade
    credit deducted; taxable_paise = base - credit; cgst/sgst/igst the GST split
    (gst_paise = cgst + sgst + igst); amount_paise = taxable + gst is what
    Razorpay charges. razorpay_payment_id stays NULL until payment (unique,
    multiple NULLs allowed). receipt is our unique reference, e.g. "st24_<client>_<ts>".
    """

    __tablename__ = "payment_orders"
    __table_args__ = (
        UniqueConstraint("razorpay_order_id", name="uq_sellertalk24_payment_orders_rzp_order"),
        UniqueConstraint("razorpay_payment_id", name="uq_sellertalk24_payment_orders_rzp_payment"),
        UniqueConstraint("receipt", name="uq_sellertalk24_payment_orders_receipt"),
        CheckConstraint(
            _in_list("status", PaymentOrderStatus.ALL),
            name="ck_sellertalk24_payment_orders_status",
        ),
        CheckConstraint(
            _in_list("purpose", PaymentPurpose.ALL),
            name="ck_sellertalk24_payment_orders_purpose",
        ),
        CheckConstraint(
            "amount_paise >= 0 AND base_paise >= 0 AND gst_paise >= 0 AND credit_paise >= 0 "
            "AND taxable_paise >= 0 AND cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_sellertalk24_payment_orders_nonneg",
        ),
        CheckConstraint(_in_list("mode", BillingMode.ALL), name="ck_sellertalk24_payment_orders_mode"),
        Index("ix_sellertalk24_payment_orders_client_status", "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("billing_plans.id"), nullable=False)
    razorpay_order_id: Mapped[str] = mapped_column(String, nullable=False)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    razorpay_signature: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    base_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # GST breakdown (decided with the GST design): stored on every payment.
    credit_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    igst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    receipt: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=PaymentOrderStatus.CREATED,
        server_default=PaymentOrderStatus.CREATED,
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_webhook: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    purpose: Mapped[str] = mapped_column(
        String, nullable=False, default=PaymentPurpose.NEW, server_default=PaymentPurpose.NEW
    )
    # 'test' (mock gateway / rzp_test_ keys) or 'live', stamped at checkout. A live process refuses to activate a
    # test order and vice versa. Pre-0065 rows are all 'test'.
    mode: Mapped[str] = mapped_column(
        String(4), nullable=False, default=BillingMode.TEST, server_default=BillingMode.TEST
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        """Short debug form: id, client, status and amount."""
        return (
            f"<PaymentOrder id={self.id} client_id={self.client_id} "
            f"status={self.status!r} amount_paise={self.amount_paise}>"
        )


class PaymentEvent(Base):
    """
    Razorpay webhook audit trail and idempotency key.

    razorpay_event_id is UNIQUE: inserting the same event twice fails/conflicts,
    so a replayed webhook can never be processed twice.
    """

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("razorpay_event_id", name="uq_sellertalk24_payment_events_event_id"),
        Index("ix_sellertalk24_payment_events_processed", "processed", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    razorpay_event_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        """Short debug form: id, event id/type and processed flag."""
        return (
            f"<PaymentEvent id={self.id} event_id={self.razorpay_event_id!r} "
            f"type={self.event_type!r} processed={self.processed}>"
        )


class ConversationUsageLog(Base):
    """
    One row per billable conversation window: (client, channel, customer, window start).

    customer_key is the customer's phone (WhatsApp), IGSID (Instagram) or
    visitor/session id (website). The unique constraint is the dedupe: the
    counter is incremented only when `INSERT ... ON CONFLICT DO NOTHING` actually
    inserts. subscription_id is NULL for usage during a trial/grace with no
    subscription row.
    """

    __tablename__ = "conversation_usage_log"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "channel", "customer_key", "window_started_at",
            name="uq_sellertalk24_conversation_usage_window",
        ),
        CheckConstraint(
            _in_list("channel", UsageChannel.ALL),
            name="ck_sellertalk24_conversation_usage_log_channel",
        ),
        Index("ix_sellertalk24_conversation_usage_log_client_created", "client_id", "created_at"),
        Index("ix_sellertalk24_conversation_usage_log_subscription", "subscription_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    subscription_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("client_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    customer_key: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        """Short debug form: id, client, channel and window start."""
        return (
            f"<ConversationUsageLog id={self.id} client_id={self.client_id} "
            f"channel={self.channel!r} window_started_at={self.window_started_at}>"
        )


class BillingAlert(Base):
    """
    A dashboard billing notification (usage thresholds, expiry reminders, grace/expired).

    (client_id, dedupe_key) is UNIQUE: the key encodes the event AND the subscription/lapse it
    belongs to (e.g. "usage_80:42"), so each alert is raised once per period. `params` holds the
    numbers for client-side translation; `message` is the English fallback.
    """

    __tablename__ = "billing_alerts"
    __table_args__ = (
        UniqueConstraint("client_id", "dedupe_key", name="uq_sellertalk24_billing_alerts_dedupe"),
        Index("ix_sellertalk24_billing_alerts_client_read", "client_id", "read_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    subscription_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("client_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False, default="info", server_default="info")
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # 'tenant' alerts show in the client's dashboard; 'admin' rows are internal flags (e.g. a partial refund to
    # review) and must never be listed to the tenant.
    audience: Mapped[str] = mapped_column(String, nullable=False, default="tenant", server_default="tenant")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        """Short debug form: id, client, kind and read state."""
        return (
            f"<BillingAlert id={self.id} client_id={self.client_id} kind={self.kind!r} "
            f"read={self.read_at is not None}>"
        )


class InvoiceCounter(Base):
    """
    Gapless invoice counter per (mode, financial year): ("live", "2026-27") -> last number issued.

    The test and live series are independent, so test payments never advance the live numbering. Allocated with
    `INSERT .. ON CONFLICT DO UPDATE .. RETURNING` (see billing.invoices), which row-locks the counter until the
    allocating transaction ends: concurrent activations queue up, and a rolled-back activation also rolls back its
    number, so numbers are never skipped (a Postgres SEQUENCE would skip them).
    """

    __tablename__ = "invoice_counters"
    __table_args__ = (CheckConstraint(_in_list("mode", BillingMode.ALL), name="ck_sellertalk24_invoice_counters_mode"),)

    mode: Mapped[str] = mapped_column(String(4), primary_key=True, default=BillingMode.TEST, server_default=BillingMode.TEST)
    financial_year: Mapped[str] = mapped_column(String(7), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        """Short debug form: mode, year and last number."""
        return f"<InvoiceCounter {self.mode} {self.financial_year} last_seq={self.last_seq}>"


class CreditNoteCounter(Base):
    """Gapless credit-note counter per (mode, financial year); same locking scheme as InvoiceCounter."""

    __tablename__ = "credit_note_counters"
    __table_args__ = (CheckConstraint(_in_list("mode", BillingMode.ALL), name="ck_sellertalk24_credit_note_counters_mode"),)

    mode: Mapped[str] = mapped_column(String(4), primary_key=True, default=BillingMode.TEST, server_default=BillingMode.TEST)
    financial_year: Mapped[str] = mapped_column(String(7), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        """Short debug form: mode, year and last number."""
        return f"<CreditNoteCounter {self.mode} {self.financial_year} last_seq={self.last_seq}>"


class Invoice(Base):
    """
    A GST tax invoice for one paid payment order. Immutable once issued: buyer/seller details and every amount
    are snapshots, so later profile or price edits never change an issued invoice. Amounts are paise.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_number", name="uq_sellertalk24_invoices_number"),
        UniqueConstraint("mode", "financial_year", "seq", name="uq_sellertalk24_invoices_mode_fy_seq"),
        CheckConstraint(_in_list("mode", BillingMode.ALL), name="ck_sellertalk24_invoices_mode"),
        UniqueConstraint("payment_order_id", name="uq_sellertalk24_invoices_payment_order"),
        Index("ix_sellertalk24_invoices_client", "client_id", "issued_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String(4), nullable=False, default=BillingMode.TEST, server_default=BillingMode.TEST)
    financial_year: Mapped[str] = mapped_column(String(7), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    payment_order_id: Mapped[int] = mapped_column(Integer, ForeignKey("payment_orders.id"), nullable=False)
    subscription_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("client_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Buyer snapshot
    buyer_name: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    buyer_email: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    buyer_gstin: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    buyer_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    buyer_state_code: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    # Seller snapshot
    seller_name: Mapped[str] = mapped_column(String, nullable=False)
    seller_gstin: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    seller_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seller_state_code: Mapped[str] = mapped_column(String(2), nullable=False)

    sac_code: Mapped[str] = mapped_column(String(8), nullable=False, default="998314", server_default="998314")
    description: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    gst_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    intra_state: Mapped[bool] = mapped_column(Boolean, nullable=False)

    base_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    igst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        """Short debug form: number, client and total."""
        return f"<Invoice {self.invoice_number} client_id={self.client_id} total_paise={self.total_paise}>"


class CreditNote(Base):
    """
    A GST credit note for one Razorpay refund (full or partial) of a paid payment order. One per refund
    (razorpay_refund_id is UNIQUE, which is what makes a redelivered refund event a no-op). The amounts are the
    refunded value split into taxable + GST in the same proportions as the order. Paise.
    """

    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint("credit_note_number", name="uq_sellertalk24_credit_notes_number"),
        UniqueConstraint("razorpay_refund_id", name="uq_sellertalk24_credit_notes_refund"),
        UniqueConstraint("mode", "financial_year", "seq", name="uq_sellertalk24_credit_notes_mode_fy_seq"),
        CheckConstraint(_in_list("mode", BillingMode.ALL), name="ck_sellertalk24_credit_notes_mode"),
        CheckConstraint("refund_kind IN ('full','partial')", name="ck_sellertalk24_credit_notes_kind"),
        CheckConstraint("total_paise > 0", name="ck_sellertalk24_credit_notes_total_pos"),
        Index("ix_sellertalk24_credit_notes_client", "client_id", "issued_at"),
        Index("ix_sellertalk24_credit_notes_order", "payment_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(4), nullable=False, default=BillingMode.TEST, server_default=BillingMode.TEST)
    credit_note_number: Mapped[str] = mapped_column(String, nullable=False)
    financial_year: Mapped[str] = mapped_column(String(7), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False)
    payment_order_id: Mapped[int] = mapped_column(Integer, ForeignKey("payment_orders.id"), nullable=False)
    invoice_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    razorpay_refund_id: Mapped[str] = mapped_column(String, nullable=False)
    refund_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    igst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        """Short debug form: number, order and total."""
        return f"<CreditNote {self.credit_note_number} order={self.payment_order_id} total_paise={self.total_paise}>"


class BillingJobRun(Base):
    """Last run of each billing background job ('maintenance', 'reconcile') — read by /health/billing."""

    __tablename__ = "billing_job_runs"

    job: Mapped[str] = mapped_column(String, primary_key=True)
    last_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String, nullable=False, default="never", server_default="never")
    detail: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    def __repr__(self) -> str:
        """Short debug form: job and status."""
        return f"<BillingJobRun {self.job} {self.last_status}>"


class BillingOpsEvent(Base):
    """
    Operational events that are not payments: rejected webhook signatures (for burst detection) and admin-alert
    dedupe markers (dedupe_key UNIQUE, so "alert once per incident" holds across workers).
    """

    __tablename__ = "billing_ops_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_sellertalk24_billing_ops_events_dedupe"),
        Index("ix_sellertalk24_billing_ops_events_kind", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detail: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        """Short debug form: id, kind and key."""
        return f"<BillingOpsEvent id={self.id} kind={self.kind!r} key={self.dedupe_key!r}>"


class BillingAdminLog(Base):
    """Audit trail of manual admin actions on billing (grants, extensions, exemptions, reprocessing)."""

    __tablename__ = "billing_admin_log"
    __table_args__ = (Index("ix_sellertalk24_billing_admin_log_client", "client_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subscription_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payment_event_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Who did it (X-Admin-User header, else "admin-key") and why — both mandatory for every mutating admin call.
    actor: Mapped[str] = mapped_column(String, nullable=False, default="admin-key", server_default="admin-key")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    detail: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
