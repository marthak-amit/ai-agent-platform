"""Add variant_material to orders table.

Revision ID: 0039_add_variant_material_to_orders
Revises: 8b144d14d7e5
Create Date: 2026-06-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0039_variant_material"
down_revision = "8b144d14d7e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("variant_material", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "variant_material")
