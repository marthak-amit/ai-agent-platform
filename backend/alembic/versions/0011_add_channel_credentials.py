"""add channel credentials to clients

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add WhatsApp and Instagram credential columns to clients."""
    op.add_column("clients", sa.Column("whatsapp_phone_number_id", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("whatsapp_access_token", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("instagram_access_token", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("instagram_account_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove channel credential columns from clients."""
    op.drop_column("clients", "instagram_account_id")
    op.drop_column("clients", "instagram_access_token")
    op.drop_column("clients", "whatsapp_access_token")
    op.drop_column("clients", "whatsapp_phone_number_id")
