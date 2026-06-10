"""Add dashboard_language to clients.

This column was originally in a duplicate 0005 migration that was never
applied because the usage_log 0005 took precedence in the migration chain.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add dashboard_language to clients with default 'en'."""
    op.add_column(
        "clients",
        sa.Column("dashboard_language", sa.String(), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    """Remove dashboard_language from clients."""
    op.drop_column("clients", "dashboard_language")
