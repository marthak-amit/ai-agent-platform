"""add pending_choice_greeting_count to conversations

BUG 2: a bare greeting ("Hi"/"Hello") sent while a multi-choice list is
pending was previously handled by the same "reject bare affirmative,
re-ask" path as garbage/typo replies, which re-dumps the identical
numbered list verbatim on every turn — a customer who keeps greeting the
bot can loop indefinitely with no acknowledgment.

pending_choice_greeting_count tracks how many consecutive bare greetings
have been seen while a choice list is open, so the handler can send a
short warm re-ask instead of the full list on the first greeting, then
give up and fall back to open intent capture after a threshold is hit
instead of repeating forever.

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add pending_choice_greeting_count to conversations."""
    op.add_column(
        "conversations",
        sa.Column(
            "pending_choice_greeting_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Drop pending_choice_greeting_count from conversations."""
    op.drop_column("conversations", "pending_choice_greeting_count")
