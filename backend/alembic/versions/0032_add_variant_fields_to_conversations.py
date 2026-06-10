"""Add selected_color and selected_size to conversations

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("selected_color", sa.String(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("selected_size", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "selected_size")
    op.drop_column("conversations", "selected_color")
