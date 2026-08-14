"""add cart columns to conversations

Phase 1 cart-based order engine. The order-collection engine previously
modeled an in-progress order as a single flat slot set per conversation
(one `selected_color`, one `selected_size`, one `pending_order_quantity`).
That breaks the moment a buyer wants more than one variant of a product in
one order: a real production case (conv=60) had a customer say "10 red and
10 pink" for one SKU, and the single `selected_color` column meant the
extractor could only ever capture one of the two colors — the other 10
units were silently dropped from the final order with no warning to the
customer or the seller.

These columns let a conversation build up a multi-line-item cart before
the order is placed, while staying NULL (and therefore inert) for the
overwhelming majority of orders that only ever need one product/variant —
those keep using the existing flat `selected_color`/`selected_size`/
`pending_order_quantity` columns exactly as before, with zero behavior
change.

cart_items:              committed line items once decided, JSON list of
                          {sku, product_name, color, size, material, qty,
                          unit_price}.
cart_variant_mode:        "same" | "different" — customer's answer to
                          "same color/size for all, or different for each?"
                          asked only when quantity > 1 on a product that
                          has variants.
cart_collection_mode:     "loop" | "batch" — set once cart_variant_mode
                          becomes "different", based on how many pieces
                          remain to collect after line item 1.
cart_wip_item:            in-progress line item being filled one attribute
                          at a time in loop mode: {color, size, material,
                          qty}, any subset present.
cart_pending_confirmation: proposed-but-unconfirmed line items awaiting an
                          echo-confirm reply, used when an LLM-inferred
                          quantity split ("half red half white") must be
                          confirmed before being committed to cart_items.

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add cart-building columns to conversations."""
    op.add_column("conversations", sa.Column("cart_items", postgresql.JSONB(), nullable=True))
    op.add_column("conversations", sa.Column("cart_variant_mode", sa.String(), nullable=True))
    op.add_column("conversations", sa.Column("cart_collection_mode", sa.String(), nullable=True))
    op.add_column("conversations", sa.Column("cart_wip_item", postgresql.JSONB(), nullable=True))
    op.add_column(
        "conversations", sa.Column("cart_pending_confirmation", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    """Drop cart-building columns from conversations."""
    op.drop_column("conversations", "cart_pending_confirmation")
    op.drop_column("conversations", "cart_wip_item")
    op.drop_column("conversations", "cart_collection_mode")
    op.drop_column("conversations", "cart_variant_mode")
    op.drop_column("conversations", "cart_items")
