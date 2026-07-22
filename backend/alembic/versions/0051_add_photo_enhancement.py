"""add product photo enhancement (style_references, product_variants fields, photo_generation_log)

Adds the schema for the "Product Photo Enhancement" feature: sellers convert a
raw flat-lay variant photo into a styled mannequin/human-model/hanging shot
via Gemini 2.5 Flash Image, picking a style from a shared style_references
library filtered by product category.

style_references is a platform-curated library, not tenant-scoped.
product_variants gains enhanced_image_url/enhanced_status/style_reference_id/
enhanced_approved — image_url itself is left untouched as the raw source photo.
photo_generation_log records one row per Gemini call (success or failure) for
per-client COGS tracking.

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-14 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create style_references + photo_generation_log; extend product_variants."""
    op.create_table(
        "style_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("style_type", sa.String(), nullable=False),
        sa.Column("reference_image_url", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_style_references_category", "style_references", ["category"])

    op.add_column("product_variants", sa.Column("enhanced_image_url", sa.String(), nullable=True))
    op.add_column("product_variants", sa.Column("enhanced_status", sa.String(), nullable=True))
    op.add_column(
        "product_variants",
        sa.Column("style_reference_id", sa.Integer(), sa.ForeignKey("style_references.id"), nullable=True),
    )
    op.add_column(
        "product_variants",
        sa.Column("enhanced_approved", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_table(
        "photo_generation_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("variant_id", sa.Integer(), sa.ForeignKey("product_variants.id"), nullable=False),
        sa.Column("style_reference_id", sa.Integer(), sa.ForeignKey("style_references.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("cost_estimate_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_photo_generation_log_client_id", "photo_generation_log", ["client_id"])
    op.create_index("ix_photo_generation_log_product_id", "photo_generation_log", ["product_id"])
    op.create_index("ix_photo_generation_log_variant_id", "photo_generation_log", ["variant_id"])


def downgrade() -> None:
    """Drop photo_generation_log, the new product_variants columns, and style_references."""
    op.drop_index("ix_photo_generation_log_variant_id", table_name="photo_generation_log")
    op.drop_index("ix_photo_generation_log_product_id", table_name="photo_generation_log")
    op.drop_index("ix_photo_generation_log_client_id", table_name="photo_generation_log")
    op.drop_table("photo_generation_log")

    op.drop_column("product_variants", "enhanced_approved")
    op.drop_column("product_variants", "style_reference_id")
    op.drop_column("product_variants", "enhanced_status")
    op.drop_column("product_variants", "enhanced_image_url")

    op.drop_index("ix_style_references_category", table_name="style_references")
    op.drop_table("style_references")
