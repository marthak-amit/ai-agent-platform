"""add catalogue fields to clients

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add public catalogue branding fields to clients table."""
    op.add_column("clients", sa.Column("catalogue_slug", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("logo_url", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("banner_url", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("catalogue_tagline", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("catalogue_theme_color", sa.String(), nullable=False, server_default="#6366F1"))
    op.create_unique_constraint("uq_clients_catalogue_slug", "clients", ["catalogue_slug"])
    op.create_index("ix_clients_catalogue_slug", "clients", ["catalogue_slug"], unique=True)


def downgrade() -> None:
    """Remove catalogue fields from clients table."""
    op.drop_index("ix_clients_catalogue_slug", table_name="clients")
    op.drop_constraint("uq_clients_catalogue_slug", "clients", type_="unique")
    op.drop_column("clients", "catalogue_theme_color")
    op.drop_column("clients", "catalogue_tagline")
    op.drop_column("clients", "banner_url")
    op.drop_column("clients", "logo_url")
    op.drop_column("clients", "catalogue_slug")
