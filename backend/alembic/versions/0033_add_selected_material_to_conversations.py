"""Add selected_material to conversations

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("selected_material", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "selected_material")
