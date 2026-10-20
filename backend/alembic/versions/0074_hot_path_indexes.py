"""Indexes for the lookups every customer message makes (open orders, message watermark, cost ledger, lead by phone)

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-20

Measured on a turn (see ``TURN_TRACE_SQL=1`` / the ``DB`` line of the TURN block): ~40 statements per message. On a remote database every
statement is a network round trip, so the statements must also be cheap on the server as the tables grow:

  orders          ix_orders_open_payment_conversation / _phone   partial (pending_payment, payment_submitted) — "this customer's unpaid orders",
                                                                  asked on every message (the ``conversation_id`` column had no index at all)
                  ix_orders_conversation_id
  payment_proofs  ix_payment_proofs_conversation_status          withdraw / "latest proof of this conversation"
  messages        ix_messages_conversation_id_id                 max(id) per conversation, ``id > mark`` ranges, newest-first
  llm_usage       ix_llm_usage_conversation_id                   the COST ledger of a turn (conversation + id > mark)
  leads           ix_leads_client_phone                          ``tag_lead`` finds a lead by (client, phone) without the channel

Plain ``CREATE INDEX IF NOT EXISTS`` (idempotent; the tables are small enough that a blocking build is milliseconds).
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

_OPEN = "status IN ('pending_payment','payment_submitted')"


def upgrade() -> None:
    """Create the hot-path indexes."""
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_orders_open_payment_conversation ON orders (client_id, conversation_id) WHERE {_OPEN}")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_orders_open_payment_phone ON orders (client_id, customer_phone) WHERE {_OPEN}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_conversation_id ON orders (conversation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_payment_proofs_conversation_status ON payment_proofs (conversation_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_messages_conversation_id_id ON messages (conversation_id, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_usage_conversation_id ON llm_usage (conversation_id, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_leads_client_phone ON leads (client_id, phone_number)")


def downgrade() -> None:
    """Drop them again."""
    for name in (
        "ix_leads_client_phone", "ix_llm_usage_conversation_id", "ix_messages_conversation_id_id", "ix_payment_proofs_conversation_status",
        "ix_orders_conversation_id", "ix_orders_open_payment_phone", "ix_orders_open_payment_conversation",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
