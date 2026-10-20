"""llm_usage: cached input tokens, Whisper audio seconds and the USD cost of every call

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-19

The ledger table ``llm_usage`` already exists (one row per provider call: purpose, model, prompt / completion tokens, ₹ cost,
latency, outcome). It gets the three things real cost tracking needs:

  cached_tokens  input tokens the provider served from its prompt cache (billed at the cached rate; included in prompt_tokens)
  audio_seconds  transcribed seconds for Whisper calls (billed per audio hour)
  cost_usd       the call's cost in USD (cost_inr = cost_usd × USD_INR at the time of the call). NULL on older rows: reports
                 fall back to cost_inr / USD_INR for those.

prompt_tokens / completion_tokens keep their names (= in_tok / out_tok; completion includes reasoning tokens).
"""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three columns (existing rows: 0 cached tokens, 0 audio seconds, unknown USD cost)."""
    op.add_column("llm_usage", sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("llm_usage", sa.Column("audio_seconds", sa.Float(), nullable=False, server_default="0"))
    op.add_column("llm_usage", sa.Column("cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the columns again."""
    op.drop_column("llm_usage", "cost_usd")
    op.drop_column("llm_usage", "audio_seconds")
    op.drop_column("llm_usage", "cached_tokens")
