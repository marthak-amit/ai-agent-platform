"""add payment_method to conversations

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-07 00:01:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add payment_method column to conversations."""
    op.add_column(
        "conversations",
        sa.Column("payment_method", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Remove payment_method column from conversations."""
    op.drop_column("conversations", "payment_method")
