"""dashboard-first onboarding: account lifecycle, demo products, sandbox orders, Google sign-in, trial claims

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-16

clients
  account_status            'setup' (pre go-live) | 'trial' | 'active' (everything else, incl. every pre-existing client)
  quick_start_completed_at  the one-screen business-name/language step; NULL = show it after first login
  bot_active                False until "Go live"; while False real customers' messages are saved but not answered
  go_live_at / trial_started_at / trial_ends_at   the trial starts at go-live, never at signup
  agent_tested_at           first time the in-dashboard simulator proved the agent answers (checklist "Test your agent")
products.is_demo            sample catalogue rows seeded for new accounts (one-click "Remove samples")
orders.is_sandbox           orders placed by the in-dashboard simulator — never listed, counted, reserved or notified
users.google_sub            the Google account linked to this login (unique); users.email_verified_at
trial_claims                one row per WhatsApp phone_number_id that has ever started a trial (UNIQUE) — 1 trial per number

Every pre-existing client is backfilled as a live, fully-onboarded account (bot_active true, quick start done,
status 'active') so nothing changes for them.
"""

from alembic import op
import sqlalchemy as sa

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the lifecycle columns, flags, Google link columns and the trial_claims table."""
    op.add_column("clients", sa.Column("account_status", sa.String(), nullable=False, server_default="active"))
    op.create_check_constraint(
        "ck_clients_account_status", "clients", "account_status IN ('setup', 'trial', 'active')"
    )
    op.add_column("clients", sa.Column("quick_start_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("bot_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("clients", sa.Column("go_live_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("agent_tested_at", sa.DateTime(timezone=True), nullable=True))
    # Existing clients are live and past onboarding: never show them the quick-start screen or a setup checklist.
    op.execute("UPDATE clients SET quick_start_completed_at = now()")

    op.add_column("products", sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("orders", sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.add_column("users", sa.Column("google_sub", sa.String(), nullable=True))
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "trial_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone_number_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("phone_number_id", name="uq_trial_claims_phone_number_id"),
    )
    op.create_index("ix_trial_claims_client_id", "trial_claims", ["client_id"])
    # Trial usage = conversation windows logged with no subscription since the trial began.
    op.create_index(
        "ix_sellertalk24_conversation_usage_log_trial",
        "conversation_usage_log",
        ["client_id", "created_at"],
        postgresql_where=sa.text("subscription_id IS NULL"),
    )


def downgrade() -> None:
    """Drop everything added in upgrade()."""
    op.drop_index("ix_sellertalk24_conversation_usage_log_trial", table_name="conversation_usage_log")
    op.drop_index("ix_trial_claims_client_id", table_name="trial_claims")
    op.drop_table("trial_claims")
    op.drop_column("users", "email_verified_at")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
    op.drop_column("orders", "is_sandbox")
    op.drop_column("products", "is_demo")
    op.drop_column("clients", "agent_tested_at")
    op.drop_column("clients", "trial_ends_at")
    op.drop_column("clients", "trial_started_at")
    op.drop_column("clients", "go_live_at")
    op.drop_column("clients", "bot_active")
    op.drop_column("clients", "quick_start_completed_at")
    op.drop_constraint("ck_clients_account_status", "clients", type_="check")
    op.drop_column("clients", "account_status")
