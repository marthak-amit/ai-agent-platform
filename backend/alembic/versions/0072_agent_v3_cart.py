"""agent v3: conversation_cart table, clients.agent_v3_enabled, backfill from the legacy slots

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-19

conversation_cart   one ACTIVE cart per conversation (partial unique index); confirmed / cancelled carts are history.
                    lines JSONB [{line_id, sku, color, size, material, qty}], customer JSONB {name, address, mobile},
                    payment_method, status, version, state JSONB (planner memory), order_id.
clients
  agent_v3_enabled  NULL = follow AGENT_V3_CLIENT_IDS (default "1"), true/false = force for this client.

Backfill — every conversation that is mid-order on the flat slots (pending_product_sku set) gets one active cart:
  * a legacy multi-line ``cart_items`` cart (migration 0057) becomes one line per item,
  * otherwise the flat slots (selected_color / size / material, pending_order_quantity) become ONE line.
The legacy columns are left untouched (the old path keeps working for clients that stay on it).
"""

import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def _stage_for(legacy_stage: str | None) -> str:
    """v3 stage that corresponds to a legacy ``current_stage`` of a conversation that is mid-order."""
    return {
        "awaiting_final_confirmation": "confirming",
    }.get(legacy_stage or "", "collecting")


def upgrade() -> None:
    """Create the cart table, add the client flag, convert in-flight legacy order drafts to carts."""
    op.add_column("clients", sa.Column("agent_v3_enabled", sa.Boolean(), nullable=True))
    op.create_table(
        "conversation_cart",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("lines", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("customer", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("payment_method", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_conversation_cart_conversation_id", "conversation_cart", ["conversation_id"])
    op.create_index("ix_conversation_cart_client_id", "conversation_cart", ["client_id"])
    op.create_index(
        "uq_conversation_cart_active", "conversation_cart", ["conversation_id"], unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    _backfill(op.get_bind())


def _backfill(bind) -> None:
    """Turn each mid-order conversation's flat slots (or legacy cart_items) into one active cart row."""
    rows = bind.execute(sa.text(
        "SELECT id, client_id, current_stage, pending_product_sku, selected_color, selected_size, selected_material, "
        "pending_order_quantity, customer_name, delivery_address, mobile_number, payment_method, cart_items "
        "FROM conversations WHERE pending_product_sku IS NOT NULL AND COALESCE(is_sandbox, false) = false"
    )).mappings().all()
    insert = sa.text(
        "INSERT INTO conversation_cart (conversation_id, client_id, lines, customer, payment_method, status, version, state) "
        "VALUES (:cid, :client_id, CAST(:lines AS jsonb), CAST(:customer AS jsonb), :pay, 'active', 1, CAST(:state AS jsonb))"
    )
    for r in rows:
        items = r["cart_items"] if isinstance(r["cart_items"], list) and r["cart_items"] else None
        if items:
            lines = [
                {
                    "line_id": uuid.uuid4().hex[:8], "sku": it.get("sku") or r["pending_product_sku"],
                    "color": it.get("color"), "size": it.get("size"), "material": it.get("material"),
                    "qty": it.get("qty"),
                }
                for it in items
            ]
        else:
            lines = [{
                "line_id": uuid.uuid4().hex[:8], "sku": r["pending_product_sku"], "color": r["selected_color"],
                "size": r["selected_size"], "material": r["selected_material"], "qty": r["pending_order_quantity"],
            }]
        bind.execute(insert, {
            "cid": r["id"], "client_id": r["client_id"], "lines": json.dumps(lines),
            "customer": json.dumps({
                "name": r["customer_name"], "address": r["delivery_address"], "mobile": r["mobile_number"],
            }),
            "pay": r["payment_method"],
            "state": json.dumps({"stage": _stage_for(r["current_stage"]), "backfilled": True}),
        })


def downgrade() -> None:
    """Drop the cart table and the client flag (the legacy slots were never modified, so nothing is lost)."""
    op.drop_index("uq_conversation_cart_active", table_name="conversation_cart")
    op.drop_index("ix_conversation_cart_client_id", table_name="conversation_cart")
    op.drop_index("ix_conversation_cart_conversation_id", table_name="conversation_cart")
    op.drop_table("conversation_cart")
    op.drop_column("clients", "agent_v3_enabled")
