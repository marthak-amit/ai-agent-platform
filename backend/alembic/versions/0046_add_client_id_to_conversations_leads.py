"""add nullable client_id to conversations/leads + channel to leads, backfill

Stage 1 of 3 (expand-migrate-contract) for the identity migration. Purely
additive: nullable FK columns + a one-time backfill. Does not touch the
existing bare phone_number unique constraint, does not add NOT NULL, and does
not change any application query code. Stage 3 (separate migration) will add
NOT NULL + drop the bare unique + add the composite unique once all NULL
client_id rows are resolved.

Backfill strategy (see migration report for full counts):
  - leads.channel = 'whatsapp' for all existing rows (platform was
    WhatsApp-only before Instagram launched).
  - conversations.client_id resolved per-row via, in priority order:
      1. orders.conversation_id -> orders.client_id (most authoritative)
      2. customers.phone -> customers.client_id, only when the phone maps to
         exactly one distinct client_id (skipped if ambiguous across clients)
    Rows with no match (no customer row, or a multi-client phone) are left
    NULL — these are Stage-3 blockers.
  - leads.client_id copied from the now-backfilled
    conversations.client_id via leads.conversation_id.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-25 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable client_id columns + leads.channel, then backfill from existing data."""
    op.add_column(
        "conversations",
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
    )
    op.create_index("ix_conversations_client_id", "conversations", ["client_id"])

    op.add_column(
        "leads",
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
    )
    op.create_index("ix_leads_client_id", "leads", ["client_id"])
    op.add_column(
        "leads",
        sa.Column("channel", sa.String(), nullable=False, server_default="whatsapp"),
    )

    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            UPDATE conversations c
            SET client_id = COALESCE(
                (SELECT o.client_id FROM orders o
                 WHERE o.conversation_id = c.id LIMIT 1),
                (SELECT cu.client_id FROM customers cu
                 WHERE cu.phone = c.phone_number
                 GROUP BY cu.client_id
                 HAVING (SELECT COUNT(DISTINCT cu2.client_id)
                         FROM customers cu2 WHERE cu2.phone = c.phone_number) = 1
                 LIMIT 1)
            )
            """
        )
    )

    conn.execute(
        sa.text(
            """
            UPDATE leads l
            SET client_id = c.client_id
            FROM conversations c
            WHERE l.conversation_id = c.id AND c.client_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    """Drop the added columns."""
    op.drop_column("leads", "channel")
    op.drop_index("ix_leads_client_id", table_name="leads")
    op.drop_column("leads", "client_id")
    op.drop_index("ix_conversations_client_id", table_name="conversations")
    op.drop_column("conversations", "client_id")
