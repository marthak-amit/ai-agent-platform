"""add users table for per-user permission system, backfill owner per client

Adds the users table backing the checklist-based permission system: one row
per person who can log into a Client's dashboard, with a role (owner/manager/
staff, used only as the invite preset template) and a JSON permissions
checklist (authoritative for non-owner access; owner bypasses it entirely).

Backfill: every existing clients row gets exactly one users row with
role='owner', copying email + hashed_password verbatim so every existing
login keeps working with no password reset. clients.email/hashed_password
are left in place (unused for auth going forward, still read by admin.py as
the business contact email) — this migration does not touch the clients
table at all.

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-14 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the users table and backfill one owner row per existing client."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="staff"),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_client_id", "users", ["client_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_unique_constraint("uq_users_email", "users", ["email"])

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO users (client_id, email, hashed_password, role, permissions, is_active, created_at)
            SELECT id, email, hashed_password, 'owner', '[]'::json, is_active, now()
            FROM clients
            """
        )
    )


def downgrade() -> None:
    """Drop the users table."""
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_client_id", table_name="users")
    op.drop_table("users")
