"""add orders table

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create orders table."""
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_number", sa.String(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_phone", sa.String(), nullable=False),
        sa.Column("delivery_address", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("product_sku", sa.String(), nullable=True),
        sa.Column("variant_color", sa.String(), nullable=True),
        sa.Column("variant_size", sa.String(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("total_amount", sa.Float(), nullable=False),
        sa.Column("payment_method", sa.String(), nullable=True, server_default="COD"),
        sa.Column("payment_status", sa.String(), nullable=True, server_default="pending"),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True, server_default="new"),
        sa.Column("tracking_number", sa.String(), nullable=True),
        sa.Column("courier_name", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("invoice_url", sa.String(), nullable=True),
        sa.Column("invoice_number", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_orders_order_number"), "orders", ["order_number"], unique=True)
    op.create_index(op.f("ix_orders_client_id"), "orders", ["client_id"], unique=False)
    op.create_index(op.f("ix_orders_customer_phone"), "orders", ["customer_phone"], unique=False)
    op.create_index(op.f("ix_orders_status"), "orders", ["status"], unique=False)


def downgrade() -> None:
    """Drop orders table."""
    op.drop_index(op.f("ix_orders_status"), table_name="orders")
    op.drop_index(op.f("ix_orders_customer_phone"), table_name="orders")
    op.drop_index(op.f("ix_orders_client_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_order_number"), table_name="orders")
    op.drop_table("orders")
