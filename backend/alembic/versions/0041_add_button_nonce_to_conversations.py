"""add button nonce to conversations

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add current_button_nonce to conversations.

    Stores a short random token that is encoded into every interactive button
    ID.  A button tap whose nonce doesn't match the stored value is silently
    rejected, making stale and double-tap button presses permanently harmless.
    """
    op.add_column(
        "conversations",
        sa.Column("current_button_nonce", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "current_button_nonce")
