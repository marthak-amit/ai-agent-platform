"""expand: add IG comment auto-reply settings, conversation source, ig_comment_replies

Adds the IG comment -> private-reply DM auto-trigger feature's schema:

- clients: ig_comment_autoreply_enabled (off by default — no existing client
  starts auto-replying silently), ig_comment_reply_all (keyword-matched is
  the default, safer than replying to everything), ig_comment_triggers
  (JSON keyword list, backfilled with the out-of-box preset),
  ig_comment_reply_text (JSON EN/HI/GU dict, backfilled with defaults).
- conversations: source (nullable, no backfill — existing rows stay NULL,
  meaning "ordinary direct-DM entry point").
- ig_comment_replies: new table. One row per comment the trigger has
  processed — serves as comment_id dedup, the rate-limit queue (rows land
  as 'pending' when the 200/hour cap is hit), and the analytics source.

Expand-only migration — purely additive, no constraint changes to existing
columns.

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-13 13:40:22.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import table, column

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_DEFAULT_TRIGGERS = ["price", "order", "want this", "how much", "available", "buy"]
_DEFAULT_REPLY_TEXT = {
    "english": "Check your DM 👀",
    "hindi": "Apna DM check karo 👀",
    "gujarati": "Tamaru DM check karo 👀",
}


def upgrade() -> None:
    """Add IG comment auto-reply columns/table."""
    op.add_column(
        "clients",
        sa.Column("ig_comment_autoreply_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "clients",
        sa.Column("ig_comment_reply_all", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "clients",
        sa.Column("ig_comment_triggers", sa.JSON(), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("ig_comment_reply_text", sa.JSON(), nullable=True),
    )

    # Backfill existing rows via bound parameters (not raw SQL string interpolation)
    # so the JSON serialization/escaping — including the emoji — is handled by the
    # dialect, not hand-quoted.
    clients_tbl = table(
        "clients",
        column("ig_comment_triggers", sa.JSON()),
        column("ig_comment_reply_text", sa.JSON()),
    )
    op.execute(
        clients_tbl.update().values(
            ig_comment_triggers=_DEFAULT_TRIGGERS,
            ig_comment_reply_text=_DEFAULT_REPLY_TEXT,
        )
    )
    op.alter_column("clients", "ig_comment_triggers", nullable=False)
    op.alter_column("clients", "ig_comment_reply_text", nullable=False)

    op.add_column(
        "conversations",
        sa.Column("source", sa.String(), nullable=True),
    )

    op.create_table(
        "ig_comment_replies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("comment_id", sa.String(), nullable=False),
        sa.Column("commenter_igsid", sa.String(), nullable=False),
        sa.Column("media_id", sa.String(), nullable=True),
        sa.Column("comment_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ig_comment_replies_client_id", "ig_comment_replies", ["client_id"])
    op.create_index(
        "ix_ig_comment_replies_comment_id", "ig_comment_replies", ["comment_id"], unique=True
    )
    op.create_index("ix_ig_comment_replies_status", "ig_comment_replies", ["status"])


def downgrade() -> None:
    """Drop IG comment auto-reply columns/table."""
    op.drop_index("ix_ig_comment_replies_status", table_name="ig_comment_replies")
    op.drop_index("ix_ig_comment_replies_comment_id", table_name="ig_comment_replies")
    op.drop_index("ix_ig_comment_replies_client_id", table_name="ig_comment_replies")
    op.drop_table("ig_comment_replies")

    op.drop_column("conversations", "source")

    op.drop_column("clients", "ig_comment_reply_text")
    op.drop_column("clients", "ig_comment_triggers")
    op.drop_column("clients", "ig_comment_reply_all")
    op.drop_column("clients", "ig_comment_autoreply_enabled")
