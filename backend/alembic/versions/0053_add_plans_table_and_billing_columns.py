"""add plans table and billing/usage columns

Moves plan definitions out of Python constants (plan_service.PLANS and
campaign_service._PLAN_LIMITS) into a DB-driven `plans` table so pricing,
conv/image quotas, and overage rates can change without a redeploy or any
edit to order_pipeline.py / billing logic.

- plans:                One row per tier (starter/growth/pro), seeded with
                         the current pricing. Folds in channel-gating and
                         campaign caps that previously lived in two separate
                         hardcoded dicts.
- clients.plan_slug:    Given a real FK to plans.plan_id (column name kept
                         to avoid touching ~10 call sites + the frontend
                         contract — it already holds plan-id values).
- clients.plan_*_snapshot / billing_cycle_start / plan_grandfathered /
  conv_limit_warned_period:
                         Snapshot of the plan's terms at the start of the
                         client's current billing cycle, so historical
                         invoices stay correct even if the live plan
                         definition changes later. Backfilled from the
                         starter plan's values so existing rows have a
                         sane baseline.
- conversations.usage_counted_period:
                         Dedupes counting the same conversation twice
                         toward a client's monthly conv_limit.
- client_monthly_usage: New table — one row per client per "YYYY-MM",
                         incremented by billing_service.
- photo_generation_log.is_overage / overage_price_inr:
                         Tags image-generation events that push a client
                         past their monthly image_quota, for future
                         overage invoicing.

Expand-only migration — purely additive, no constraint changes to existing
columns other than the new plan_slug FK (values already satisfy it).

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add plans table, snapshot/usage columns, and client_monthly_usage."""
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("price_inr", sa.Integer(), nullable=False),
        sa.Column("conv_limit", sa.Integer(), nullable=False),
        sa.Column("image_quota", sa.Integer(), nullable=False),
        sa.Column("image_overage_price", sa.Integer(), nullable=False),
        sa.Column("daily_msg_limit", sa.Integer(), nullable=False),
        sa.Column("channels", postgresql.JSONB(), nullable=False),
        sa.Column("campaign_allowed", sa.Boolean(), nullable=False),
        sa.Column("campaign_max_recipients", sa.Integer(), nullable=False),
        sa.Column("campaign_monthly_limit", sa.Integer(), nullable=False),
        sa.Column("tier_order", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        INSERT INTO plans (
            plan_id, name, price_inr, conv_limit, image_quota, image_overage_price,
            daily_msg_limit, channels, campaign_allowed, campaign_max_recipients,
            campaign_monthly_limit, tier_order, description
        ) VALUES
        (
            'starter', 'Starter', 1499, 700, 20, 8,
            100, '["whatsapp"]', false, 0,
            0, 0, 'Perfect for small businesses getting started with WhatsApp automation.'
        ),
        (
            'growth', 'Growth', 3999, 2000, 50, 6,
            300, '["whatsapp", "instagram"]', true, 500,
            1, 1, 'Scale your reach across WhatsApp and Instagram.'
        ),
        (
            'pro', 'Pro', 9999, 6000, 100, 5,
            700, '["whatsapp", "instagram", "website"]', true, 99999,
            99999, 2, 'Full omnichannel presence — WhatsApp, Instagram, and website widget.'
        )
        """
    )

    op.create_foreign_key(
        "fk_clients_plan_slug_plans",
        "clients",
        "plans",
        ["plan_slug"],
        ["plan_id"],
    )

    op.add_column(
        "clients",
        sa.Column("plan_conv_limit_snapshot", sa.Integer(), nullable=False, server_default="700"),
    )
    op.add_column(
        "clients",
        sa.Column("plan_price_snapshot", sa.Integer(), nullable=False, server_default="1499"),
    )
    op.add_column(
        "clients",
        sa.Column("plan_image_quota_snapshot", sa.Integer(), nullable=False, server_default="20"),
    )
    op.add_column(
        "clients",
        sa.Column(
            "plan_image_overage_price_snapshot", sa.Integer(), nullable=False, server_default="8"
        ),
    )
    op.add_column("clients", sa.Column("billing_cycle_start", sa.Date(), nullable=True))
    op.add_column(
        "clients",
        sa.Column("plan_grandfathered", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "clients", sa.Column("conv_limit_warned_period", sa.String(), nullable=True)
    )

    # Backfill snapshot values per-client's actual current plan (not just
    # starter's defaults) and start their cycle from their signup date.
    op.execute(
        """
        UPDATE clients c
        SET
            plan_conv_limit_snapshot = p.conv_limit,
            plan_price_snapshot = p.price_inr,
            plan_image_quota_snapshot = p.image_quota,
            plan_image_overage_price_snapshot = p.image_overage_price,
            billing_cycle_start = COALESCE(c.created_at::date, CURRENT_DATE)
        FROM plans p
        WHERE p.plan_id = c.plan_slug
        """
    )

    op.add_column(
        "conversations", sa.Column("usage_counted_period", sa.String(length=7), nullable=True)
    )

    op.create_table(
        "client_monthly_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False, index=True
        ),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("conv_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "period", name="uq_client_monthly_usage_client_period"),
    )

    op.add_column(
        "photo_generation_log",
        sa.Column("is_overage", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "photo_generation_log", sa.Column("overage_price_inr", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    """Reverse all changes made by upgrade(), in opposite order."""
    op.drop_column("photo_generation_log", "overage_price_inr")
    op.drop_column("photo_generation_log", "is_overage")

    op.drop_table("client_monthly_usage")

    op.drop_column("conversations", "usage_counted_period")

    op.drop_column("clients", "conv_limit_warned_period")
    op.drop_column("clients", "plan_grandfathered")
    op.drop_column("clients", "billing_cycle_start")
    op.drop_column("clients", "plan_image_overage_price_snapshot")
    op.drop_column("clients", "plan_image_quota_snapshot")
    op.drop_column("clients", "plan_price_snapshot")
    op.drop_column("clients", "plan_conv_limit_snapshot")

    op.drop_constraint("fk_clients_plan_slug_plans", "clients", type_="foreignkey")

    op.drop_table("plans")
