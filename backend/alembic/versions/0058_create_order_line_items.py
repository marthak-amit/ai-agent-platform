"""create order_line_items table

Phase 1 cart-based order engine. `orders` previously modeled one row as
exactly one product/variant/quantity — there was no way to represent a
single purchase containing several line items (e.g. 40 Red/L + 30 Blue/M
of the same saree, or two different products bought together). This table
makes `orders` the purchase/cart header (one `order_number` per checkout)
and `order_line_items` the child rows, one per distinct product/variant in
that cart.

The existing flat columns on `orders` (product_name, product_sku,
variant_color, variant_size, variant_material, quantity, unit_price) are
kept and, from this point on, are populated from line item 1 of the cart —
this is deliberate denormalization so the seller dashboard
(frontend/src/pages/Orders.tsx) and CSV export, which read those flat
columns directly, keep working unchanged for Phase 1. `orders.total_amount`
changes meaning (code-level only, no schema change) from "this order's
total" to "the cart's grand total across all line items" — this is what
keeps revenue/stats aggregation in order_service.get_orders_stats()
correct with zero code changes there.

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create order_line_items table."""
    op.create_table(
        "order_line_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("product_sku", sa.String(), nullable=True),
        sa.Column("variant_color", sa.String(), nullable=True),
        sa.Column("variant_size", sa.String(), nullable=True),
        sa.Column("variant_material", sa.String(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("subtotal", sa.Float(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_order_line_items_order_id", "order_line_items", ["order_id"])


def downgrade() -> None:
    """Drop order_line_items table."""
    op.drop_index("ix_order_line_items_order_id", table_name="order_line_items")
    op.drop_table("order_line_items")
