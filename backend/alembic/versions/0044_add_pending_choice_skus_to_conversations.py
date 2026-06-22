"""Add pending_choice_skus to conversations — tracks an open "which one?"
multi-option list so a bare affirmative is rejected/re-asked instead of being
repinned from a stale last_shown_sku.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("pending_choice_skus", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "pending_choice_skus")
