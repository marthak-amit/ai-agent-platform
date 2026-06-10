"""add GST fields to clients

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add gst_number, business_address, hsn_code to clients."""
    op.add_column("clients", sa.Column("gst_number", sa.String(), nullable=True))
    op.add_column("clients", sa.Column("business_address", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("hsn_code", sa.String(), nullable=False, server_default="5007"))


def downgrade() -> None:
    """Remove GST fields from clients."""
    op.drop_column("clients", "hsn_code")
    op.drop_column("clients", "business_address")
    op.drop_column("clients", "gst_number")
