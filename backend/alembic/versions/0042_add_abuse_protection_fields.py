"""add abuse protection fields to conversations

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add abuse-protection columns to conversations table."""
    op.add_column("conversations", sa.Column("slot_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversations", sa.Column("slot_attempt_slot", sa.String(), nullable=True))
    op.add_column("conversations", sa.Column("off_topic_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversations", sa.Column("llm_calls_today", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversations", sa.Column("llm_calls_date", sa.String(10), nullable=True))


def downgrade() -> None:
    """Remove abuse-protection columns from conversations table."""
    op.drop_column("conversations", "llm_calls_date")
    op.drop_column("conversations", "llm_calls_today")
    op.drop_column("conversations", "off_topic_count")
    op.drop_column("conversations", "slot_attempt_slot")
    op.drop_column("conversations", "slot_attempt_count")
