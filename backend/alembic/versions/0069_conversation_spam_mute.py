"""conversations: temporary mute after repeated irrelevant messages

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-14

`spam_muted_until`: once a customer has sent `irrelevant_streak_limit` irrelevant messages in a row (emoji-only,
off-topic), the bot stops answering free text — no LLM call, no reply — until this time. NULL = not muted.
"""

from alembic import op
import sqlalchemy as sa

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable mute timestamp."""
    op.add_column("conversations", sa.Column("spam_muted_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the mute timestamp."""
    op.drop_column("conversations", "spam_muted_until")
