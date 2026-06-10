"""add conversation flow and order fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add sales funnel tracking fields to conversations."""
    op.add_column(
        "conversations",
        sa.Column(
            "current_stage",
            sa.String(),
            nullable=False,
            server_default="greeting",
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "order_intent_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("customer_name", sa.String(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("delivery_address", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("pending_order_quantity", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove sales funnel fields from conversations."""
    op.drop_column("conversations", "pending_order_quantity")
    op.drop_column("conversations", "delivery_address")
    op.drop_column("conversations", "customer_name")
    op.drop_column("conversations", "order_intent_score")
    op.drop_column("conversations", "current_stage")
