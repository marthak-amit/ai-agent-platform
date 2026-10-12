"""customers: one row per (client_id, phone)

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-12

A customer's first two messages arriving together both created a profile (the table only had a non-unique
index), after which every lookup by (client_id, phone) raised MultipleResultsFound and the customer's later
messages failed. Existing duplicates are merged into the oldest row (stats summed, first non-null profile
fields kept) and the pair becomes UNIQUE; the service layer now inserts through a SAVEPOINT (add_or_get).
"""

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge duplicate customer rows, then add the unique constraint."""
    op.execute(
        """
        WITH ranked AS (
            SELECT id, client_id, phone,
                   MIN(id) OVER (PARTITION BY client_id, phone) AS keep_id,
                   COUNT(*) OVER (PARTITION BY client_id, phone) AS n
            FROM customers
        ),
        totals AS (
            SELECT r.keep_id,
                   SUM(c.total_orders) AS total_orders,
                   SUM(c.total_spent) AS total_spent,
                   MAX(c.last_message_at) AS last_message_at
            FROM ranked r JOIN customers c ON c.id = r.id
            WHERE r.n > 1
            GROUP BY r.keep_id
        )
        UPDATE customers c
           SET total_orders = t.total_orders,
               total_spent = t.total_spent,
               last_message_at = COALESCE(t.last_message_at, c.last_message_at)
          FROM totals t
         WHERE c.id = t.keep_id
        """
    )
    op.execute(
        """
        DELETE FROM customers c
         USING (SELECT id, MIN(id) OVER (PARTITION BY client_id, phone) AS keep_id FROM customers) r
         WHERE c.id = r.id AND r.id <> r.keep_id
        """
    )
    op.create_unique_constraint("uq_customers_client_phone", "customers", ["client_id", "phone"])


def downgrade() -> None:
    """Drop the unique constraint (merged rows are not split back)."""
    op.drop_constraint("uq_customers_client_phone", "customers", type_="unique")
