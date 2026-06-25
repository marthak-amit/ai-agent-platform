"""expand: add mobile_number delivery field to conversations and orders

Adds a nullable, unconstrained `mobile_number` column to `conversations`
and `orders`.

This is DELIVERY data (where the order ships / how the rider contacts the
customer) — it is NOT identity. The identity key remains the existing
composite (client_id, channel, phone_number) from migration 0047, which is
untouched here. mobile_number has no unique constraint and is never used
for conversation lookup.

Channel behavior (enforced in app code, not the schema):
  - WhatsApp: auto-filled from the WA sender phone number.
  - Instagram: collected as a normal order slot (IGSID is not a phone).

Expand-only migration — purely additive, no backfill, no constraint changes.

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable mobile_number columns to conversations and orders."""
    op.add_column(
        "conversations",
        sa.Column("mobile_number", sa.String(), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("mobile_number", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop mobile_number columns from conversations and orders."""
    op.drop_column("orders", "mobile_number")
    op.drop_column("conversations", "mobile_number")
