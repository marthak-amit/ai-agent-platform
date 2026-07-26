"""add flow_state_at/last_context/last_context_at to conversations

Adds the schema for time-based flow-state expiry: a customer who goes silent
mid-flow and returns later must not have a stale current_stage/pending
product resumed against them.

- flow_state_at:   stamped every time current_stage is persisted
                    (conversation_service.update_stage). Backfilled from
                    updated_at/created_at so existing rows are immediately
                    eligible for expiry rather than being treated as
                    infinitely fresh.
- last_context:     JSONB snapshot of the last product referenced
                    ({product_id, sku, name, price}) — survives a
                    flow_state reset so pronoun references ("that", "it")
                    keep resolving for longer (CONTEXT_TTL) than the active
                    flow itself (FLOW_STATE_TTL). No backfill: existing
                    rows simply have no snapshot until the customer's next
                    product pin.
- last_context_at:  timestamp paired with last_context, same reasoning.

Expand-only migration — purely additive, no constraint changes to existing
columns.

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add flow_state_at/last_context/last_context_at to conversations."""
    op.add_column(
        "conversations",
        sa.Column("flow_state_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_context", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_context_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill flow_state_at for existing rows so the TTL check applies
    # immediately instead of treating every pre-existing conversation as
    # having no flow_state (i.e. never-expiring) until its next turn.
    op.execute(
        "UPDATE conversations SET flow_state_at = COALESCE(updated_at, created_at) "
        "WHERE flow_state_at IS NULL"
    )


def downgrade() -> None:
    """Drop last_context_at/last_context/flow_state_at from conversations."""
    op.drop_column("conversations", "last_context_at")
    op.drop_column("conversations", "last_context")
    op.drop_column("conversations", "flow_state_at")
