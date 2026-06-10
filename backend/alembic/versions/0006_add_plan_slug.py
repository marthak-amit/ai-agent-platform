"""Add plan_slug column to clients.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add plan_slug VARCHAR(50) DEFAULT 'starter' to clients table."""
    op.add_column(
        "clients",
        sa.Column(
            "plan_slug",
            sa.String(length=50),
            nullable=False,
            server_default="starter",
        ),
    )


def downgrade() -> None:
    """Remove plan_slug from clients table."""
    op.drop_column("clients", "plan_slug")
