"""add clients.router_v2_enabled (per-client LLM intent-router flag)

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable per-client override (NULL = follow the ROUTER_V2_CLIENT_IDS env default)."""
    op.add_column("clients", sa.Column("router_v2_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop the per-client router flag."""
    op.drop_column("clients", "router_v2_enabled")
