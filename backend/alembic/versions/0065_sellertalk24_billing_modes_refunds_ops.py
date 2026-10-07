"""SellerTalk24 billing: test/live mode separation, credit notes, revoked status, admin audit + ops tables

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-09

* `mode` ('test'|'live') on payment_orders and invoices; every existing row is test data (default 'test').
* invoice_counters / new credit_note_counters keyed by (mode, financial_year). Existing invoice numbers get the
  "TEST-" prefix so the live series ("ST24/2026-27/0001") can start at 0001 without a unique-number clash.
* credit_notes (one per Razorpay refund), subscription status 'revoked' + revoked_at.
* billing_alerts.audience ('tenant'|'admin') so admin flags never show up in a tenant's dashboard.
* billing_admin_log.actor / reason.
* billing_job_runs (last run of each billing job) and billing_ops_events (bad-signature log + admin-alert dedupe).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'pending','active','expired','cancelled','superseded'"
_NEW_STATUSES = "'pending','active','expired','cancelled','superseded','revoked'"


def _mode_col() -> sa.Column:
    """A NOT NULL mode column; the 'test' default marks every pre-existing row as test data."""
    return sa.Column("mode", sa.String(4), nullable=False, server_default="test")


def upgrade() -> None:
    """Apply the schema + data changes described in the module docstring."""
    # --- mode on orders + invoices ---------------------------------------------------------
    for table in ("payment_orders", "invoices"):
        op.add_column(table, _mode_col())
        op.create_check_constraint(f"ck_sellertalk24_{table}_mode", table, "mode IN ('test','live')")

    # --- invoice counters per (mode, financial_year) ---------------------------------------
    op.add_column("invoice_counters", _mode_col())
    op.drop_constraint("invoice_counters_pkey", "invoice_counters", type_="primary")
    op.create_primary_key("invoice_counters_pkey", "invoice_counters", ["mode", "financial_year"])
    op.create_check_constraint("ck_sellertalk24_invoice_counters_mode", "invoice_counters", "mode IN ('test','live')")

    op.drop_constraint("uq_sellertalk24_invoices_fy_seq", "invoices", type_="unique")
    op.create_unique_constraint("uq_sellertalk24_invoices_mode_fy_seq", "invoices", ["mode", "financial_year", "seq"])
    # Existing numbers belong to the test series: re-label them so "ST24/…/0001" is free for live.
    op.execute("UPDATE invoices SET invoice_number = 'TEST-' || invoice_number WHERE invoice_number NOT LIKE 'TEST-%'")

    # --- credit notes ----------------------------------------------------------------------
    op.create_table(
        "credit_note_counters",
        _mode_col(),
        sa.Column("financial_year", sa.String(7), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("mode", "financial_year", name="credit_note_counters_pkey"),
        sa.CheckConstraint("mode IN ('test','live')", name="ck_sellertalk24_credit_note_counters_mode"),
    )
    op.create_table(
        "credit_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        _mode_col(),
        sa.Column("credit_note_number", sa.String(), nullable=False),
        sa.Column("financial_year", sa.String(7), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("payment_order_id", sa.Integer(), sa.ForeignKey("payment_orders.id"), nullable=False),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("razorpay_refund_id", sa.String(), nullable=False),
        sa.Column("refund_kind", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("taxable_paise", sa.Integer(), nullable=False),
        sa.Column("cgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("igst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("credit_note_number", name="uq_sellertalk24_credit_notes_number"),
        sa.UniqueConstraint("razorpay_refund_id", name="uq_sellertalk24_credit_notes_refund"),
        sa.UniqueConstraint("mode", "financial_year", "seq", name="uq_sellertalk24_credit_notes_mode_fy_seq"),
        sa.CheckConstraint("mode IN ('test','live')", name="ck_sellertalk24_credit_notes_mode"),
        sa.CheckConstraint("refund_kind IN ('full','partial')", name="ck_sellertalk24_credit_notes_kind"),
        sa.CheckConstraint("total_paise > 0", name="ck_sellertalk24_credit_notes_total_pos"),
    )
    op.create_index("ix_sellertalk24_credit_notes_client", "credit_notes", ["client_id", "issued_at"])
    op.create_index("ix_sellertalk24_credit_notes_order", "credit_notes", ["payment_order_id"])

    # --- subscriptions: revoked ------------------------------------------------------------
    op.drop_constraint("ck_sellertalk24_client_subscriptions_status", "client_subscriptions", type_="check")
    op.create_check_constraint(
        "ck_sellertalk24_client_subscriptions_status", "client_subscriptions", f"status IN ({_NEW_STATUSES})"
    )
    op.add_column("client_subscriptions", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))

    # --- alerts: tenant vs admin audience --------------------------------------------------
    op.add_column("billing_alerts", sa.Column("audience", sa.String(), nullable=False, server_default="tenant"))
    op.create_check_constraint("ck_sellertalk24_billing_alerts_audience", "billing_alerts", "audience IN ('tenant','admin')")

    # --- admin audit: who + why ------------------------------------------------------------
    op.add_column("billing_admin_log", sa.Column("actor", sa.String(), nullable=False, server_default="admin-key"))
    op.add_column("billing_admin_log", sa.Column("reason", sa.Text(), nullable=False, server_default=""))

    # --- ops tables ------------------------------------------------------------------------
    op.create_table(
        "billing_job_runs",
        sa.Column("job", sa.String(), primary_key=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(), nullable=False, server_default="never"),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table(
        "billing_ops_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_sellertalk24_billing_ops_events_dedupe"),
    )
    op.create_index("ix_sellertalk24_billing_ops_events_kind", "billing_ops_events", ["kind", "created_at"])


def downgrade() -> None:
    """Reverse upgrade(). Revoked subscriptions become 'cancelled'; live-series rows would collide, so refuse if any."""
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM payment_orders WHERE mode = 'live'")):
        raise RuntimeError("refusing to downgrade 0065: live-mode billing rows exist")
    op.drop_index("ix_sellertalk24_billing_ops_events_kind", table_name="billing_ops_events")
    op.drop_table("billing_ops_events")
    op.drop_table("billing_job_runs")
    op.drop_column("billing_admin_log", "reason")
    op.drop_column("billing_admin_log", "actor")
    op.drop_constraint("ck_sellertalk24_billing_alerts_audience", "billing_alerts", type_="check")
    op.drop_column("billing_alerts", "audience")
    op.drop_column("client_subscriptions", "revoked_at")
    op.execute("UPDATE client_subscriptions SET status = 'cancelled' WHERE status = 'revoked'")
    op.drop_constraint("ck_sellertalk24_client_subscriptions_status", "client_subscriptions", type_="check")
    op.create_check_constraint(
        "ck_sellertalk24_client_subscriptions_status", "client_subscriptions", f"status IN ({_OLD_STATUSES})"
    )
    op.drop_index("ix_sellertalk24_credit_notes_order", table_name="credit_notes")
    op.drop_index("ix_sellertalk24_credit_notes_client", table_name="credit_notes")
    op.drop_table("credit_notes")
    op.drop_table("credit_note_counters")
    op.execute("UPDATE invoices SET invoice_number = substr(invoice_number, 6) WHERE invoice_number LIKE 'TEST-%'")
    op.drop_constraint("uq_sellertalk24_invoices_mode_fy_seq", "invoices", type_="unique")
    op.create_unique_constraint("uq_sellertalk24_invoices_fy_seq", "invoices", ["financial_year", "seq"])
    op.drop_constraint("ck_sellertalk24_invoice_counters_mode", "invoice_counters", type_="check")
    op.drop_constraint("invoice_counters_pkey", "invoice_counters", type_="primary")
    op.create_primary_key("invoice_counters_pkey", "invoice_counters", ["financial_year"])
    op.drop_column("invoice_counters", "mode")
    for table in ("invoices", "payment_orders"):
        op.drop_constraint(f"ck_sellertalk24_{table}_mode", table, type_="check")
        op.drop_column(table, "mode")
