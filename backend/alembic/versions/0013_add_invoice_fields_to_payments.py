"""add invoice fields to payments

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add invoice_url, customer_name, customer_address to payments."""
    op.add_column("payments", sa.Column("invoice_url", sa.String(), nullable=True))
    op.add_column("payments", sa.Column("customer_name", sa.String(), nullable=True))
    op.add_column("payments", sa.Column("customer_address", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove invoice fields from payments."""
    op.drop_column("payments", "customer_address")
    op.drop_column("payments", "customer_name")
    op.drop_column("payments", "invoice_url")
