"""Add extended payment fields to clients table.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"  # chains after 0036_add_delivery_time_fields
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("upi_display_name", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("cod_limit", sa.Integer(), nullable=True))
    op.add_column("clients", sa.Column("accepts_upi", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("clients", sa.Column("accepts_bank_transfer", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("clients", sa.Column("bank_account_name", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("bank_account_number", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("bank_ifsc", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("razorpay_key_id", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("razorpay_key_secret", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("payment_instructions", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "payment_instructions")
    op.drop_column("clients", "razorpay_key_secret")
    op.drop_column("clients", "razorpay_key_id")
    op.drop_column("clients", "bank_ifsc")
    op.drop_column("clients", "bank_account_number")
    op.drop_column("clients", "bank_account_name")
    op.drop_column("clients", "accepts_bank_transfer")
    op.drop_column("clients", "accepts_upi")
    op.drop_column("clients", "cod_limit")
    op.drop_column("clients", "upi_display_name")
