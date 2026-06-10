"""Add onboarding fields to clients table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add business_type, business_description, products, whatsapp_number, api_key to clients."""
    op.add_column("clients", sa.Column("business_type", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("business_description", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("products", sa.JSON(), nullable=True))
    op.add_column("clients", sa.Column("whatsapp_number", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("api_key", sa.String(), nullable=True))
    op.create_index("ix_clients_api_key", "clients", ["api_key"], unique=True)


def downgrade() -> None:
    """Remove onboarding columns from clients table."""
    op.drop_index("ix_clients_api_key", table_name="clients")
    op.drop_column("clients", "api_key")
    op.drop_column("clients", "whatsapp_number")
    op.drop_column("clients", "products")
    op.drop_column("clients", "business_description")
    op.drop_column("clients", "business_type")
