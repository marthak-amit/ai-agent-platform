"""clients: cached shop profile for the out-of-scope boundary

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-13

`shop_context` is the short text the router / off-topic check reads to know what the business sells;
`shop_context_keywords` its keyword set (free in-scope pre-check); `shop_context_hash` fingerprints the
inputs (business type/description + catalogue categories/names) so a catalogue edit triggers a rebuild;
`shop_context_updated_at` bounds how stale the knowledge-base topics can get. All nullable: NULL = build lazily.
"""

from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the four shop-profile columns."""
    op.add_column("clients", sa.Column("shop_context", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("shop_context_keywords", sa.JSON(), nullable=True))
    op.add_column("clients", sa.Column("shop_context_hash", sa.String(length=40), nullable=True))
    op.add_column("clients", sa.Column("shop_context_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the shop-profile columns."""
    op.drop_column("clients", "shop_context_updated_at")
    op.drop_column("clients", "shop_context_hash")
    op.drop_column("clients", "shop_context_keywords")
    op.drop_column("clients", "shop_context")
