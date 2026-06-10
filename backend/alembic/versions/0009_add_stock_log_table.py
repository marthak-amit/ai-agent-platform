"""add stock_logs table

Revision ID: 0009
Revises: 0008
Create Date: 2025-01-01 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create stock_logs table for inventory adjustment history."""
    op.create_table(
        "stock_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("adjustment", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default="correction"),
        sa.Column("stock_before", sa.Integer(), nullable=False),
        sa.Column("stock_after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_stock_logs_product_id", "stock_logs", ["product_id"])
    op.create_index("ix_stock_logs_client_id", "stock_logs", ["client_id"])


def downgrade() -> None:
    """Drop stock_logs table."""
    op.drop_index("ix_stock_logs_client_id", table_name="stock_logs")
    op.drop_index("ix_stock_logs_product_id", table_name="stock_logs")
    op.drop_table("stock_logs")
