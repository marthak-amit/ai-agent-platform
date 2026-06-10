"""add briefing fields to clients

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add briefing_enabled and briefing_time to clients."""
    op.add_column(
        "clients",
        sa.Column("briefing_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "clients",
        sa.Column("briefing_time", sa.String(), nullable=False, server_default="09:00"),
    )


def downgrade() -> None:
    """Remove briefing fields from clients."""
    op.drop_column("clients", "briefing_time")
    op.drop_column("clients", "briefing_enabled")
