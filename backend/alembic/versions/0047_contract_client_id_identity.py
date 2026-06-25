"""contract: enforce client_id identity on conversations/leads

Stage 3 of 3 (expand-migrate-contract) for the identity migration. Now that
client_id is fully backfilled (zero NULLs) and the app filters by client_id,
tighten the constraints to make tenant scoping authoritative:

  - conversations: client_id NOT NULL; add composite unique
    (client_id, channel, phone_number). phone_number column name unchanged.
  - leads: client_id NOT NULL, channel NOT NULL; drop the bare
    phone_number unique index/constraint; add composite unique
    (client_id, channel, phone_number).

Precondition (verified before writing this migration): zero NULL client_id
rows on either table, and zero duplicate (client_id, channel, phone_number)
groups on either table.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enforce NOT NULL + composite-unique identity on conversations/leads."""
    op.alter_column("conversations", "client_id", nullable=False)
    op.create_unique_constraint(
        "uq_conversations_client_channel_phone",
        "conversations",
        ["client_id", "channel", "phone_number"],
    )

    op.alter_column("leads", "client_id", nullable=False)
    op.alter_column("leads", "channel", nullable=False)
    op.drop_index("ix_leads_phone_number", table_name="leads")
    op.create_unique_constraint(
        "uq_leads_client_channel_phone",
        "leads",
        ["client_id", "channel", "phone_number"],
    )


def downgrade() -> None:
    """Revert to nullable client_id and the bare phone_number unique."""
    op.drop_constraint("uq_leads_client_channel_phone", "leads", type_="unique")
    op.create_index("ix_leads_phone_number", "leads", ["phone_number"], unique=True)
    op.alter_column("leads", "channel", nullable=True)
    op.alter_column("leads", "client_id", nullable=True)

    op.drop_constraint("uq_conversations_client_channel_phone", "conversations", type_="unique")
    op.alter_column("conversations", "client_id", nullable=True)
