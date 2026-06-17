"""add order idempotency key

Revision ID: 0040
Revises: 0039_add_variant_material_to_orders
Create Date: 2026-06-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "f58c66651b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add idempotency_key to orders — unique, nullable.

    Keyed on the WhatsApp message ID (wamid) of the triggering message.
    Prevents double order-insert on webhook retry.
    """
    op.add_column(
        "orders",
        sa.Column("idempotency_key", sa.String(250), nullable=True),
    )
    op.create_unique_constraint(
        "uq_orders_idempotency_key", "orders", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_orders_idempotency_key", "orders", type_="unique")
    op.drop_column("orders", "idempotency_key")
