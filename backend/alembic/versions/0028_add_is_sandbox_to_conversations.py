"""add is_sandbox to conversations

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add is_sandbox column to conversations table."""
    op.add_column(
        "conversations",
        sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    """Remove is_sandbox column from conversations table."""
    op.drop_column("conversations", "is_sandbox")
