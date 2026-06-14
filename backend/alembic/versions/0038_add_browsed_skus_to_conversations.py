"""Add browsed_skus to conversations for cross-sell tracking.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("browsed_skus", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "browsed_skus")
