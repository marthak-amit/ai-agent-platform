"""add payment settings to clients

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add accepts_cod and upi_id columns to clients."""
    op.add_column(
        "clients",
        sa.Column("accepts_cod", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "clients",
        sa.Column("upi_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Remove payment settings columns from clients."""
    op.drop_column("clients", "accepts_cod")
    op.drop_column("clients", "upi_id")
