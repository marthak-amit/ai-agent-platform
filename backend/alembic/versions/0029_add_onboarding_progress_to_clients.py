"""add onboarding progress to clients

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-08 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add onboarding_step and onboarding_completed columns to clients."""
    op.add_column(
        "clients",
        sa.Column("onboarding_step", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "clients",
        sa.Column("onboarding_completed", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    """Remove onboarding progress columns from clients."""
    op.drop_column("clients", "onboarding_step")
    op.drop_column("clients", "onboarding_completed")
