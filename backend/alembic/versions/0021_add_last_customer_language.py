"""add last_customer_language to conversations

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add last_customer_language column for sticky language detection."""
    op.add_column(
        "conversations",
        sa.Column(
            "last_customer_language",
            sa.String(),
            nullable=False,
            server_default="english",
        ),
    )


def downgrade() -> None:
    """Remove last_customer_language column."""
    op.drop_column("conversations", "last_customer_language")
