"""add product_variants table

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create product_variants table and add has_variants flag to products."""
    op.create_table(
        "product_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("size", sa.String(), nullable=True),
        sa.Column("material", sa.String(), nullable=True),
        sa.Column("sku", sa.String(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_product_variants_product_id", "product_variants", ["product_id"])
    op.create_index("ix_product_variants_client_id", "product_variants", ["client_id"])
    op.create_index("ix_product_variants_sku", "product_variants", ["sku"])

    op.add_column(
        "products",
        sa.Column("has_variants", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop product_variants table and has_variants column."""
    op.drop_column("products", "has_variants")
    op.drop_index("ix_product_variants_sku", table_name="product_variants")
    op.drop_index("ix_product_variants_client_id", table_name="product_variants")
    op.drop_index("ix_product_variants_product_id", table_name="product_variants")
    op.drop_table("product_variants")
