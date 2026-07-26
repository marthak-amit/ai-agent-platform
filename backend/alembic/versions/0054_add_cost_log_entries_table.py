"""add cost_log_entries table

Persists app.services.cost_log entries to Postgres instead of keeping them
only in an in-process dict, which was lost on every restart/deploy and
invisible across multiple Railway workers. Columns mirror the existing
in-memory entry shape (direction/path/model/token counts/cost/call_kind)
unchanged — this only adds persistence, not new fields.

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create cost_log_entries."""
    op.create_table(
        "cost_log_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.String(), nullable=False, server_default="TEMPLATE"),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("in_tok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("out_tok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("call_kind", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cost_log_entries_conversation_id", "cost_log_entries", ["conversation_id"])


def downgrade() -> None:
    """Drop cost_log_entries."""
    op.drop_index("ix_cost_log_entries_conversation_id", table_name="cost_log_entries")
    op.drop_table("cost_log_entries")
