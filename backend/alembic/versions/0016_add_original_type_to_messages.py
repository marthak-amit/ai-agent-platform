"""Add original_type column to messages table.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable original_type column to messages."""
    op.add_column("messages", sa.Column("original_type", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove original_type column from messages."""
    op.drop_column("messages", "original_type")
