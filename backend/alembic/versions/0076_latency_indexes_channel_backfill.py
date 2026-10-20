"""v1.7 latency: composite indexes for the per-turn lookups, one-time messages.channel backfill

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-20

* ``messages.channel`` used to be back-filled by an ``UPDATE … WHERE channel IS NULL`` at the end of EVERY turn
  (``payment_inbound.post_process``). Rows are now written with their channel; this migration fixes the old ones once.
* Composite indexes for the queries every turn runs: open orders of a conversation, the active/non-active cart of a conversation,
  the tenant's active products / variants (catalogue snapshot + stock refresh).

Existing indexes already cover ``messages(conversation_id, created_at)``, ``llm_usage(conversation_id, id)`` and
``stock_reservations(product_id, status)``, so those are not repeated. All statements are idempotent.
"""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_orders_client_conversation_status", "orders", "client_id, conversation_id, status"),
    ("ix_conversation_cart_conversation_status", "conversation_cart", "conversation_id, status"),
    ("ix_product_variants_client_active", "product_variants", "client_id, is_active"),
    ("ix_products_client_active", "products", "client_id, is_active"),
)


def upgrade() -> None:
    """Back-fill message channels once and add the composite indexes."""
    op.execute(
        "UPDATE messages m SET channel = c.channel FROM conversations c "
        "WHERE m.conversation_id = c.id AND m.channel IS NULL AND c.channel IS NOT NULL"
    )
    for name, table, columns in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")


def downgrade() -> None:
    """Drop the indexes (the channel back-fill is data, not schema: nothing to undo)."""
    for name, _table, _columns in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
