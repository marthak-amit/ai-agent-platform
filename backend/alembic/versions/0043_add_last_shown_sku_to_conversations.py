"""Add last_shown_sku to conversations — survives the post-order reset so a
bare "yes" after a product card can be repinned without an LLM call.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("last_shown_sku", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "last_shown_sku")
