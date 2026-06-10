"""enhance products table — add sku, category, is_active, low_stock_alert

Revision ID: 0008
Revises: 0007
Create Date: 2025-01-01 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add sku, category, is_active, low_stock_alert columns to products."""
    op.add_column("products", sa.Column("sku", sa.String(), nullable=True))
    op.add_column("products", sa.Column("category", sa.String(), nullable=True))
    op.add_column(
        "products",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "products",
        sa.Column("low_stock_alert", sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_index("ix_products_sku", "products", ["sku"])
    op.create_index("ix_products_category", "products", ["category"])


def downgrade() -> None:
    """Remove the four enhanced catalogue columns."""
    op.drop_index("ix_products_category", table_name="products")
    op.drop_index("ix_products_sku", table_name="products")
    op.drop_column("products", "low_stock_alert")
    op.drop_column("products", "is_active")
    op.drop_column("products", "category")
    op.drop_column("products", "sku")
