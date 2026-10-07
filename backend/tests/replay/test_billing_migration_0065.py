"""
Migration 0065 on a database that already holds billing data: every existing row becomes test data, existing
invoice numbers move to the TEST- series (so live can start at ST24/<FY>/0001), constraints accept the new values,
and downgrade/upgrade round-trips. Uses its own throwaway database, built at revision 0064.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from tests.replay.conftest import _ADMIN_SYNC_URL, _PG_AVAILABLE, BACKEND_DIR

DB = "replay_mig_0065"
ASYNC_URL = f"postgresql+asyncpg://bytes-amit@localhost/{DB}"
SYNC_URL = f"postgresql://bytes-amit@localhost/{DB}"

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


def alembic(*args: str) -> None:
    """Run alembic against the throwaway DB; fail loudly with its output."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=str(BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": ASYNC_URL}, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def engine():
    """A database migrated to 0064 and seeded with billing rows, as production would look before 0065."""
    admin = sa.create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{DB}"'))
    alembic("upgrade", "0064")
    eng = sa.create_engine(SYNC_URL)
    with eng.begin() as conn:
        conn.execute(sa.text("SET session_replication_role = replica"))        # no clients needed for this data test
        plan_id = conn.scalar(sa.text("SELECT id FROM billing_plans WHERE code = 'starter_1500'"))
        for n in (1, 2):
            conn.execute(sa.text(
                "INSERT INTO payment_orders (client_id, plan_id, razorpay_order_id, receipt, amount_paise, base_paise, status) "
                "VALUES (7, :p, :o, :r, 542682, 459900, 'paid')"), {"p": plan_id, "o": f"order_old_{n}", "r": f"rcpt_{n}"})
            conn.execute(sa.text(
                "INSERT INTO invoices (invoice_number, financial_year, seq, client_id, payment_order_id, issued_at, seller_name, "
                "seller_state_code, description, gst_rate_bps, intra_state, base_paise, taxable_paise, total_paise) "
                "VALUES (:num, '2026-27', :seq, 7, (SELECT id FROM payment_orders WHERE razorpay_order_id = :o), now(), 'SellerTalk24', "
                "'24', 'old', 1800, true, 459900, 459900, 542682)"),
                {"num": f"ST24/2026-27/{n:04d}", "seq": n, "o": f"order_old_{n}"})
        conn.execute(sa.text("INSERT INTO invoice_counters (financial_year, last_seq) VALUES ('2026-27', 2)"))
        conn.execute(sa.text(
            "INSERT INTO client_subscriptions (client_id, plan_id, status, current_period_start, current_period_end, conversation_limit) "
            "VALUES (7, :p, 'active', now(), now() + interval '30 days', 1500)"), {"p": plan_id})
    yield eng
    eng.dispose()
    with sa.create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT").connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))


def test_upgrade_marks_existing_rows_as_test_and_moves_invoice_numbers_to_the_test_series(engine):
    alembic("upgrade", "0065")
    with engine.connect() as conn:
        assert conn.scalars(sa.text("SELECT DISTINCT mode FROM payment_orders")).all() == ["test"]
        assert conn.scalars(sa.text("SELECT DISTINCT mode FROM invoices")).all() == ["test"]
        assert conn.scalars(sa.text("SELECT invoice_number FROM invoices ORDER BY seq")).all() == [
            "TEST-ST24/2026-27/0001", "TEST-ST24/2026-27/0002"]
        assert conn.execute(sa.text("SELECT mode, financial_year, last_seq FROM invoice_counters")).all() == [("test", "2026-27", 2)]
        assert conn.scalar(sa.text("SELECT status FROM client_subscriptions")) == "active"


def test_the_live_series_starts_at_0001_regardless_of_the_test_history(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("SET session_replication_role = replica"))
        # what allocate_invoice_number does for a live invoice in the same year
        seq = conn.scalar(sa.text(
            "INSERT INTO invoice_counters (mode, financial_year, last_seq) VALUES ('live', '2026-27', 1) "
            "ON CONFLICT (mode, financial_year) DO UPDATE SET last_seq = invoice_counters.last_seq + 1 RETURNING last_seq"))
        assert seq == 1
        plan_id = conn.scalar(sa.text("SELECT id FROM billing_plans WHERE code = 'starter_1500'"))
        conn.execute(sa.text(
            "INSERT INTO payment_orders (client_id, plan_id, razorpay_order_id, receipt, amount_paise, base_paise, status, mode) "
            "VALUES (7, :p, 'order_live_1', 'rcpt_live_1', 542682, 459900, 'paid', 'live')"), {"p": plan_id})
        conn.execute(sa.text(
            "INSERT INTO invoices (invoice_number, mode, financial_year, seq, client_id, payment_order_id, issued_at, seller_name, "
            "seller_state_code, description, gst_rate_bps, intra_state, base_paise, taxable_paise, total_paise) "
            "VALUES ('ST24/2026-27/0001', 'live', '2026-27', 1, 7, (SELECT id FROM payment_orders WHERE razorpay_order_id='order_live_1'), "
            "now(), 'SellerTalk24', '24', 'live', 1800, true, 1, 1, 1)"))
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM invoices WHERE mode='live'")) == 1
        assert conn.scalar(sa.text("SELECT last_seq FROM invoice_counters WHERE mode='test'")) == 2    # test series untouched


def test_new_constraints_accept_revoked_and_reject_nonsense(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE client_subscriptions SET status = 'revoked', revoked_at = now()"))
    bad = (
        "UPDATE client_subscriptions SET status = 'bogus'",
        "UPDATE payment_orders SET mode = 'beta'",
        "UPDATE invoices SET mode = 'beta'",
        "INSERT INTO billing_alerts (client_id, kind, dedupe_key, title, message, audience) VALUES (1,'k','d','t','m','public')",
    )
    for statement in bad:
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text("SET session_replication_role = replica"))
            conn.execute(sa.text(statement))


def test_downgrade_refuses_while_live_orders_exist_and_round_trips_otherwise(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE payment_orders SET mode='live' WHERE razorpay_order_id='order_old_1'"))
    with pytest.raises(AssertionError, match="refusing to downgrade 0065"):
        alembic("downgrade", "0064")
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM invoices WHERE mode='live'"))
        conn.execute(sa.text("DELETE FROM payment_orders WHERE razorpay_order_id='order_live_1'"))
        conn.execute(sa.text("UPDATE payment_orders SET mode='test'"))
        conn.execute(sa.text("DELETE FROM invoice_counters WHERE mode='live'"))
        conn.execute(sa.text("UPDATE client_subscriptions SET status='active', revoked_at=NULL"))
    alembic("downgrade", "0064")
    with engine.connect() as conn:
        assert conn.scalars(sa.text("SELECT invoice_number FROM invoices ORDER BY seq")).all() == ["ST24/2026-27/0001", "ST24/2026-27/0002"]
    alembic("upgrade", "0065")
    with engine.connect() as conn:
        assert conn.scalars(sa.text("SELECT invoice_number FROM invoices ORDER BY seq")).all() == [
            "TEST-ST24/2026-27/0001", "TEST-ST24/2026-27/0002"]
