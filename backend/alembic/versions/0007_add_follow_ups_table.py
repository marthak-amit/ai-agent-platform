"""add follow_ups table

Revision ID: 0007
Revises: 0006
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create follow_ups table."""
    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id"),
            nullable=False,
        ),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="sent"),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_follow_ups_lead_id", "follow_ups", ["lead_id"])
    op.create_index("ix_follow_ups_phone_number", "follow_ups", ["phone_number"])
    op.create_index("ix_follow_ups_sent_at", "follow_ups", ["sent_at"])


def downgrade() -> None:
    """Drop follow_ups table."""
    op.drop_index("ix_follow_ups_sent_at", table_name="follow_ups")
    op.drop_index("ix_follow_ups_phone_number", table_name="follow_ups")
    op.drop_index("ix_follow_ups_lead_id", table_name="follow_ups")
    op.drop_table("follow_ups")
