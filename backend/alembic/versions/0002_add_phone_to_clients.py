"""Add phone column to clients.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable phone column to clients table."""
    op.add_column("clients", sa.Column("phone", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove phone column from clients table."""
    op.drop_column("clients", "phone")
