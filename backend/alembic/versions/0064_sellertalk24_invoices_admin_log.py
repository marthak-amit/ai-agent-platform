"""SellerTalk24 billing: invoices, per-financial-year invoice counters, admin action log

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create invoice_counters, invoices (one per paid payment order) and billing_admin_log."""
    op.create_table(
        "invoice_counters",
        sa.Column("financial_year", sa.String(7), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_number", sa.String(), nullable=False),
        sa.Column("financial_year", sa.String(7), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("payment_order_id", sa.Integer(), sa.ForeignKey("payment_orders.id"), nullable=False),
        sa.Column(
            "subscription_id", sa.Integer(),
            sa.ForeignKey("client_subscriptions.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("buyer_name", sa.String(), nullable=False, server_default=""),
        sa.Column("buyer_email", sa.String(), nullable=False, server_default=""),
        sa.Column("buyer_gstin", sa.String(), nullable=True),
        sa.Column("buyer_address", sa.Text(), nullable=True),
        sa.Column("buyer_state_code", sa.String(2), nullable=True),
        sa.Column("seller_name", sa.String(), nullable=False),
        sa.Column("seller_gstin", sa.String(), nullable=True),
        sa.Column("seller_address", sa.Text(), nullable=True),
        sa.Column("seller_state_code", sa.String(2), nullable=False),
        sa.Column("sac_code", sa.String(8), nullable=False, server_default="998314"),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("gst_rate_bps", sa.Integer(), nullable=False),
        sa.Column("intra_state", sa.Boolean(), nullable=False),
        sa.Column("base_paise", sa.Integer(), nullable=False),
        sa.Column("credit_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("taxable_paise", sa.Integer(), nullable=False),
        sa.Column("cgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("igst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("invoice_number", name="uq_sellertalk24_invoices_number"),
        sa.UniqueConstraint("financial_year", "seq", name="uq_sellertalk24_invoices_fy_seq"),
        sa.UniqueConstraint("payment_order_id", name="uq_sellertalk24_invoices_payment_order"),
    )
    op.create_index("ix_sellertalk24_invoices_client", "invoices", ["client_id", "issued_at"])

    op.create_table(
        "billing_admin_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("payment_event_id", sa.Integer(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sellertalk24_billing_admin_log_client", "billing_admin_log", ["client_id", "created_at"])


def downgrade() -> None:
    """Drop the tables added in upgrade()."""
    op.drop_index("ix_sellertalk24_billing_admin_log_client", table_name="billing_admin_log")
    op.drop_table("billing_admin_log")
    op.drop_index("ix_sellertalk24_invoices_client", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("invoice_counters")
