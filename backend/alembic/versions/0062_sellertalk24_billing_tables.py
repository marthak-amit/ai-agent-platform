"""SellerTalk24 prepaid billing: billing_plans, payment_orders, client_subscriptions,
payment_events, conversation_usage_log (+ idempotent plan seed)

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-06
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

# (code, name, conversation_limit, price_paise [GST-exclusive], features, sort_order)
_PLAN_SEED = [
    ("starter_1500", "Starter", 1500, 459900, {"whatsapp": True, "instagram": False}, 1),
    ("growth_5000", "Growth", 5000, 1199900, {"whatsapp": True, "instagram": True}, 2),
    ("pro_9000", "Pro", 9000, 1799900, {"whatsapp": True, "instagram": True}, 3),
]

_SUBSCRIPTION_STATUSES = "'pending','active','expired','cancelled','superseded'"
_ORDER_STATUSES = "'created','attempted','paid','failed','refunded'"
_PURPOSES = "'new','renewal','upgrade'"
_CHANNELS = "'whatsapp','instagram','website'"


def _ts(name: str, *, nullable: bool = False, server_default=True) -> sa.Column:
    """A timezone-aware timestamp column defaulting to now()."""
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if server_default else None,
    )


def _seed_plans(bind) -> None:
    """Idempotently upsert the seeded plans, keyed on code (re-running corrects drifted values)."""
    upsert = sa.text(
        """
        INSERT INTO billing_plans
            (code, name, conversation_limit, price_paise, currency,
             billing_period_days, features, is_active, sort_order)
        VALUES
            (:code, :name, :conversation_limit, :price_paise, 'INR',
             30, CAST(:features AS jsonb), true, :sort_order)
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            conversation_limit = EXCLUDED.conversation_limit,
            price_paise = EXCLUDED.price_paise,
            features = EXCLUDED.features,
            sort_order = EXCLUDED.sort_order,
            updated_at = now()
        """
    )
    for code, name, limit, price, features, order in _PLAN_SEED:
        bind.execute(
            upsert,
            {
                "code": code,
                "name": name,
                "conversation_limit": limit,
                "price_paise": price,
                "features": json.dumps(features),
                "sort_order": order,
            },
        )


def upgrade() -> None:
    """Create the five billing tables and upsert the three seeded plans."""
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("conversation_limit", sa.Integer(), nullable=False),
        sa.Column("price_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("billing_period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "features", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("code", name="uq_sellertalk24_billing_plans_code"),
        sa.CheckConstraint("price_paise >= 0", name="ck_sellertalk24_billing_plans_price_nonneg"),
        sa.CheckConstraint("conversation_limit > 0", name="ck_sellertalk24_billing_plans_limit_pos"),
        sa.CheckConstraint(
            "billing_period_days > 0", name="ck_sellertalk24_billing_plans_period_pos"
        ),
    )

    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("billing_plans.id"), nullable=False),
        sa.Column("razorpay_order_id", sa.String(), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column("razorpay_signature", sa.String(), nullable=True),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("base_paise", sa.Integer(), nullable=False),
        sa.Column("gst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credit_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("taxable_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("igst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("receipt", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="created"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("raw_webhook", postgresql.JSONB(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False, server_default="new"),
        _ts("created_at"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("razorpay_order_id", name="uq_sellertalk24_payment_orders_rzp_order"),
        sa.UniqueConstraint("razorpay_payment_id", name="uq_sellertalk24_payment_orders_rzp_payment"),
        sa.UniqueConstraint("receipt", name="uq_sellertalk24_payment_orders_receipt"),
        sa.CheckConstraint(
            f"status IN ({_ORDER_STATUSES})", name="ck_sellertalk24_payment_orders_status"
        ),
        sa.CheckConstraint(
            f"purpose IN ({_PURPOSES})", name="ck_sellertalk24_payment_orders_purpose"
        ),
        sa.CheckConstraint(
            "amount_paise >= 0 AND base_paise >= 0 AND gst_paise >= 0 AND credit_paise >= 0 "
            "AND taxable_paise >= 0 AND cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_sellertalk24_payment_orders_nonneg",
        ),
    )
    op.create_index(
        "ix_sellertalk24_payment_orders_client_status", "payment_orders", ["client_id", "status"]
    )

    op.create_table(
        "client_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("billing_plans.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversation_limit", sa.Integer(), nullable=False),
        sa.Column("conversations_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credited_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source_payment_id", sa.Integer(), sa.ForeignKey("payment_orders.id"), nullable=True
        ),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            f"status IN ({_SUBSCRIPTION_STATUSES})",
            name="ck_sellertalk24_client_subscriptions_status",
        ),
        sa.CheckConstraint(
            "current_period_end > current_period_start",
            name="ck_sellertalk24_client_subscriptions_period_order",
        ),
        sa.CheckConstraint(
            "conversations_used >= 0 AND credited_paise >= 0",
            name="ck_sellertalk24_client_subscriptions_nonneg",
        ),
    )
    op.create_index(
        "uq_sellertalk24_client_subscriptions_one_active",
        "client_subscriptions",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_sellertalk24_client_subscriptions_client_status",
        "client_subscriptions",
        ["client_id", "status"],
    )
    op.create_index(
        "ix_sellertalk24_client_subscriptions_status_period_end",
        "client_subscriptions",
        ["status", "current_period_end"],
    )

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("razorpay_event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("razorpay_event_id", name="uq_sellertalk24_payment_events_event_id"),
    )
    op.create_index(
        "ix_sellertalk24_payment_events_processed", "payment_events", ["processed", "created_at"]
    )

    op.create_table(
        "conversation_usage_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("client_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("customer_key", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint(
            "client_id", "channel", "customer_key", "window_started_at",
            name="uq_sellertalk24_conversation_usage_window",
        ),
        sa.CheckConstraint(
            f"channel IN ({_CHANNELS})", name="ck_sellertalk24_conversation_usage_log_channel"
        ),
    )
    op.create_index(
        "ix_sellertalk24_conversation_usage_log_client_created",
        "conversation_usage_log",
        ["client_id", "created_at"],
    )
    op.create_index(
        "ix_sellertalk24_conversation_usage_log_subscription",
        "conversation_usage_log",
        ["subscription_id"],
    )

    _seed_plans(op.get_bind())


def downgrade() -> None:
    """Drop the billing tables (children first); the seeded plans go with billing_plans."""
    op.drop_table("conversation_usage_log")
    op.drop_table("payment_events")
    op.drop_table("client_subscriptions")
    op.drop_table("payment_orders")
    op.drop_table("billing_plans")
