"""chat flow hardening: cancel confirmation, draft restore, last search, side-topic memory, high-value review

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-17

conversations
  last_cancelled_draft  JSONB   {ts, fields{...}} snapshot of the order draft taken whenever a draft/order is cancelled
                                in chat; "continue" within 30 min restores it (order_recovery.py)
  cancel_confirm_at     tz      set while the bot waits for the customer's Yes/No on "Cancel this order?"
  last_search           JSONB   {query, filters, shown_skus, ts} of the last catalogue search, so "another one?" /
                                "show me more" continues the same search instead of re-pinning a product
  last_side_topic       JSONB   {topic, n, ts} of the last side question (e.g. discount) for follow-ups like "please"
clients
  high_value_order_threshold  NULL = platform default (HIGH_VALUE_ORDER_THRESHOLD, ₹50,000)
orders
  requires_manual_review      set when the total exceeds the threshold: the seller must acknowledge before approving
  manual_review_cleared_at    when the seller acknowledged the review
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable flow-memory columns and the high-value review flag."""
    op.add_column("conversations", sa.Column("last_cancelled_draft", JSONB(), nullable=True))
    op.add_column("conversations", sa.Column("cancel_confirm_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("last_search", JSONB(), nullable=True))
    op.add_column("conversations", sa.Column("last_side_topic", JSONB(), nullable=True))
    op.add_column("clients", sa.Column("high_value_order_threshold", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("requires_manual_review", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("orders", sa.Column("manual_review_cleared_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the columns again."""
    op.drop_column("orders", "manual_review_cleared_at")
    op.drop_column("orders", "requires_manual_review")
    op.drop_column("clients", "high_value_order_threshold")
    op.drop_column("conversations", "last_side_topic")
    op.drop_column("conversations", "last_search")
    op.drop_column("conversations", "cancel_confirm_at")
    op.drop_column("conversations", "last_cancelled_draft")
