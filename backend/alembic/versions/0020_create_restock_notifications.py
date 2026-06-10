"""create restock_notifications table

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create restock_notifications table for customer back-in-stock alerts."""
    op.create_table(
        "restock_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("customer_phone", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_restock_notifications_client_id", "restock_notifications", ["client_id"])
    op.create_index("ix_restock_notifications_product_id", "restock_notifications", ["product_id"])


def downgrade() -> None:
    """Drop restock_notifications table."""
    op.drop_index("ix_restock_notifications_product_id", table_name="restock_notifications")
    op.drop_index("ix_restock_notifications_client_id", table_name="restock_notifications")
    op.drop_table("restock_notifications")
