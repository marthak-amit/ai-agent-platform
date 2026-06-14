"""Add last_followup_sku and followup_sent_at to conversations.

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("last_followup_sku", sa.String(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("followup_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "followup_sent_at")
    op.drop_column("conversations", "last_followup_sku")
