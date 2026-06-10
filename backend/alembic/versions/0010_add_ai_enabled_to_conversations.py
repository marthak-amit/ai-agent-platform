"""add ai_enabled, taken_over_at, taken_over_note to conversations

Revision ID: 0010
Revises: 0009
Create Date: 2025-01-01 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add human-takeover control fields to conversations."""
    op.add_column(
        "conversations",
        sa.Column(
            "ai_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("taken_over_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("taken_over_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove human-takeover fields from conversations."""
    op.drop_column("conversations", "taken_over_note")
    op.drop_column("conversations", "taken_over_at")
    op.drop_column("conversations", "ai_enabled")
