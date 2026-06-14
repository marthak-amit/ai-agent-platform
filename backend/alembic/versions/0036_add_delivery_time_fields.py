"""add delivery time fields to clients and products

Revision ID: 0036
Revises: 8b144d14d7e5
Create Date: 2026-06-14 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "8b144d14d7e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add delivery_days_min/max to clients, delivery_days to products."""
    op.add_column("clients", sa.Column("delivery_days_min", sa.Integer(), nullable=True, server_default="3"))
    op.add_column("clients", sa.Column("delivery_days_max", sa.Integer(), nullable=True, server_default="7"))
    op.add_column("products", sa.Column("delivery_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove delivery time columns."""
    op.drop_column("products", "delivery_days")
    op.drop_column("clients", "delivery_days_max")
    op.drop_column("clients", "delivery_days_min")
