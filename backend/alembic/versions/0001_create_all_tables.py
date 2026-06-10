"""Create all tables.

Revision ID: 0001
Revises:
Create Date: 2026-05-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create conversations, messages, leads, payments, and clients tables."""
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("business_name", sa.String(), nullable=False, server_default=""),
        sa.Column("gemini_system_prompt", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clients_email", "clients", ["email"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False, server_default="whatsapp"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_phone_number", "conversations", ["phone_number"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="cold"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leads_phone_number", "leads", ["phone_number"], unique=True)

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("qr_code_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="created"),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payments_phone_number", "payments", ["phone_number"])
    op.create_index("ix_payments_qr_code_id", "payments", ["qr_code_id"], unique=True)


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    op.drop_index("ix_payments_qr_code_id", table_name="payments")
    op.drop_index("ix_payments_phone_number", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_leads_phone_number", table_name="leads")
    op.drop_table("leads")
    op.drop_table("messages")
    op.drop_index("ix_conversations_phone_number", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_clients_email", table_name="clients")
    op.drop_table("clients")
