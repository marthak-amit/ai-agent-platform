"""Add pending_product_sku to conversations

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("pending_product_sku", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "pending_product_sku")
