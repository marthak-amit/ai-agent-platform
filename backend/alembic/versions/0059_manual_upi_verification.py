"""manual UPI payment verification + dashboard outbound messaging

We never collect money: the customer pays the seller's UPI directly and
sends a screenshot in chat; the seller approves/rejects it from the
dashboard. This migration adds:

  * payment_proofs         — one row per screenshot the customer sent
  * order_audit_log        — who moved an order between states, and when
  * stock_reservations     — stock reserved at pending_payment, consumed at
                             paid, released at cancelled
  * message_templates      — approved Meta templates usable outside the 24h
                             window (listed in the WINDOW_CLOSED error)
  * messages.*             — direction / channel / sender_type / media so
                             every inbound+outbound message (incl. media) is
                             persisted and renderable in the dashboard inbox
  * conversations.*        — bot pause bookkeeping (auto-resume), read marker,
                             throttle stamp for the "verification in progress" ack
  * clients.*              — static UPI QR, bot auto-resume minutes, payment
                             expiry hours, "UPI missing" dashboard alert stamp
  * orders.*               — payment_submitted_at / cancelled_at / cancel_reason

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create payment/audit/reservation/template tables and extend existing ones."""
    # ── payment_proofs ───────────────────────────────────────────────────────
    op.create_table(
        "payment_proofs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_by_name", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_proofs_order_id", "payment_proofs", ["order_id"])
    op.create_index("ix_payment_proofs_client_status", "payment_proofs", ["client_id", "status"])

    # ── order_audit_log ──────────────────────────────────────────────────────
    op.create_table(
        "order_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_name", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_audit_log_order_id", "order_audit_log", ["order_id"])

    # ── stock_reservations ───────────────────────────────────────────────────
    op.create_table(
        "stock_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("variant_id", sa.Integer(), sa.ForeignKey("product_variants.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stock_reservations_order_id", "stock_reservations", ["order_id"])
    op.create_index("ix_stock_reservations_product_status", "stock_reservations", ["product_id", "status"])

    # ── message_templates ────────────────────────────────────────────────────
    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False, server_default="en"),
        sa.Column("category", sa.String(), nullable=False, server_default="utility"),
        sa.Column("status", sa.String(), nullable=False, server_default="approved"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("client_id", "name", "language", name="uq_message_templates_client_name_lang"),
    )
    op.create_index("ix_message_templates_client_id", "message_templates", ["client_id"])

    # ── messages ─────────────────────────────────────────────────────────────
    op.add_column("messages", sa.Column("direction", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("channel", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("sender_type", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("sender_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("messages", sa.Column("media_url", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("media_type", sa.String(), nullable=True))
    # Backfill from the legacy role column + the owning conversation's channel.
    op.execute(
        "UPDATE messages SET direction = CASE WHEN role = 'user' THEN 'inbound' ELSE 'outbound' END, "
        "sender_type = CASE role WHEN 'user' THEN 'customer' WHEN 'human' THEN 'human' ELSE 'bot' END"
    )
    op.execute(
        "UPDATE messages m SET channel = c.channel FROM conversations c WHERE m.conversation_id = c.id"
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    # ── conversations ────────────────────────────────────────────────────────
    op.add_column("conversations", sa.Column("bot_paused_at", sa.DateTime(timezone=True), nullable=True))
    # 'human_send' (auto-resumes after idle) | 'manual' | 'escalation'
    op.add_column("conversations", sa.Column("bot_pause_source", sa.String(), nullable=True))
    op.add_column("conversations", sa.Column("human_last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("last_proof_ack_at", sa.DateTime(timezone=True), nullable=True))

    # ── clients ──────────────────────────────────────────────────────────────
    op.add_column("clients", sa.Column("upi_qr_url", sa.Text(), nullable=True))
    op.add_column(
        "clients",
        sa.Column("bot_auto_resume_minutes", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "clients",
        sa.Column("payment_expiry_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.add_column("clients", sa.Column("payment_setup_alert_at", sa.DateTime(timezone=True), nullable=True))

    # ── orders ───────────────────────────────────────────────────────────────
    op.add_column("orders", sa.Column("payment_submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancel_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    """Reverse upgrade()."""
    for col in ("cancel_reason", "cancelled_at", "payment_submitted_at"):
        op.drop_column("orders", col)
    for col in ("payment_setup_alert_at", "payment_expiry_hours", "bot_auto_resume_minutes", "upi_qr_url"):
        op.drop_column("clients", col)
    for col in ("last_proof_ack_at", "last_read_at", "human_last_activity_at", "bot_pause_source", "bot_paused_at"):
        op.drop_column("conversations", col)
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    for col in ("media_type", "media_url", "sender_user_id", "sender_type", "channel", "direction"):
        op.drop_column("messages", col)
    op.drop_table("message_templates")
    op.drop_table("stock_reservations")
    op.drop_table("order_audit_log")
    op.drop_table("payment_proofs")
