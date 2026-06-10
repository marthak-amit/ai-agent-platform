"""add stock_deducted to orders

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add stock_deducted boolean column to orders (default False)."""
    op.add_column(
        "orders",
        sa.Column("stock_deducted", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    """Remove stock_deducted column from orders."""
    op.drop_column("orders", "stock_deducted")
