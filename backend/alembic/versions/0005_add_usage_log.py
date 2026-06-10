"""Add usage_logs table and daily_message_limit to clients.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add daily_message_limit to clients and create usage_logs table."""
    op.add_column(
        "clients",
        sa.Column("daily_message_limit", sa.Integer(), nullable=False, server_default="100"),
    )

    op.create_table(
        "usage_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "date", name="uq_usage_client_date"),
    )
    op.create_index("ix_usage_logs_client_id", "usage_logs", ["client_id"])


def downgrade() -> None:
    """Remove usage_logs table and daily_message_limit from clients."""
    op.drop_index("ix_usage_logs_client_id", table_name="usage_logs")
    op.drop_table("usage_logs")
    op.drop_column("clients", "daily_message_limit")
