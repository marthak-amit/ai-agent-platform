"""add send-gate fields to customers (opt-out + 24h-window snapshot)

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add opted_out / optout_confirmed_at / last_inbound_at and backfill.

    last_inbound_at is backfilled from the messages table (latest role='user'
    message per (client_id, phone_number) conversation pair) so the send
    gate's 24h-window fallback is correct from the first deploy instead of
    treating every existing customer as window-closed-unknown.
    """
    op.add_column(
        "customers",
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "customers",
        sa.Column("optout_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE customers cu
        SET last_inbound_at = s.last_at
        FROM (
            SELECT conv.client_id AS client_id,
                   conv.phone_number AS phone,
                   MAX(m.created_at) AS last_at
            FROM messages m
            JOIN conversations conv ON conv.id = m.conversation_id
            WHERE m.role = 'user'
              AND conv.client_id IS NOT NULL
            GROUP BY conv.client_id, conv.phone_number
        ) s
        WHERE cu.client_id = s.client_id
          AND cu.phone = s.phone
        """
    )


def downgrade() -> None:
    """Remove send-gate columns from customers."""
    op.drop_column("customers", "last_inbound_at")
    op.drop_column("customers", "optout_confirmed_at")
    op.drop_column("customers", "opted_out")
