"""add customers table

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the customers table."""
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("total_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_spent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("preferred_language", sa.String(), nullable=False, server_default="english"),
        sa.Column("preferred_payment", sa.String(), nullable=True),
        sa.Column("is_vip", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("tags", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_customers_client_id", "customers", ["client_id"])
    op.create_index("ix_customers_phone", "customers", ["phone"])
    op.create_unique_constraint("uq_customers_client_phone", "customers", ["client_id", "phone"])


def downgrade() -> None:
    """Drop the customers table."""
    op.drop_table("customers")
