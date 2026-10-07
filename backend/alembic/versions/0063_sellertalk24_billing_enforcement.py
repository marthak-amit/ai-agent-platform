"""SellerTalk24 billing enforcement: client_subscriptions.over_limit, clients.billing_exempt
(client 1 exempt), billing_alerts

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the soft-cap flag, the exemption flag (+ data migration for client 1) and the alerts table."""
    op.add_column(
        "client_subscriptions",
        sa.Column("over_limit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "clients",
        sa.Column("billing_exempt", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Data migration: the house account (Riya Sarees, client_id=1) is never billed or restricted.
    op.execute("UPDATE clients SET billing_exempt = true WHERE id = 1")

    op.create_table(
        "billing_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column(
            "subscription_id", sa.Integer(),
            sa.ForeignKey("client_subscriptions.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "dedupe_key", name="uq_sellertalk24_billing_alerts_dedupe"),
    )
    op.create_index(
        "ix_sellertalk24_billing_alerts_client_read", "billing_alerts", ["client_id", "read_at", "created_at"]
    )


def downgrade() -> None:
    """Drop the alerts table and both new columns."""
    op.drop_table("billing_alerts")
    op.drop_column("clients", "billing_exempt")
    op.drop_column("client_subscriptions", "over_limit")
