"""create llm_usage table (per-call LLM token/cost/latency telemetry)

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create llm_usage with indexes for per-client/day, per-conversation and per-order rollups."""
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_inr", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_usage_client_created", "llm_usage", ["client_id", "created_at"])
    op.create_index("ix_llm_usage_conversation", "llm_usage", ["conversation_id"])
    op.create_index("ix_llm_usage_order", "llm_usage", ["order_id"])


def downgrade() -> None:
    """Drop llm_usage and its indexes."""
    op.drop_index("ix_llm_usage_order", table_name="llm_usage")
    op.drop_index("ix_llm_usage_conversation", table_name="llm_usage")
    op.drop_index("ix_llm_usage_client_created", table_name="llm_usage")
    op.drop_table("llm_usage")
