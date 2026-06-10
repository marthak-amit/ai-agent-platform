"""Add summary_shown to conversations

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = ("0030", "adc44d46353f")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "summary_shown",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "summary_shown")
