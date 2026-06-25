"""create demo_leads table

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-22 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create demo_leads table for public marketing-site demo requests."""
    op.create_table(
        "demo_leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("whatsapp_number", sa.String(), nullable=True),
        sa.Column("monthly_order_volume", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="marketing_site"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_demo_leads_email", "demo_leads", ["email"])


def downgrade() -> None:
    """Drop demo_leads table."""
    op.drop_index("ix_demo_leads_email", table_name="demo_leads")
    op.drop_table("demo_leads")
