"""Add interrupted_sku to conversations

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("interrupted_sku", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "interrupted_sku")
