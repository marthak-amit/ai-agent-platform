#!/usr/bin/env python3
"""Reset a test conversation to a clean greeting state.

Run between WhatsApp test runs so each flow starts fresh instead of inheriting
slots from a previous (often buggy) session.

Usage:
    python reset_conv.py                         # resets conv id 52, purges test orders
    python reset_conv.py --id 52
    python reset_conv.py --phone 917575092467
    python reset_conv.py --id 52 --no-purge-orders   # skip order purge

If you just added a model column, run `alembic upgrade head` first.
"""
import argparse
import asyncio

from sqlalchemy import delete, select

from app.db import _get_session_factory
from app.models.conversation import Conversation
from app.models.order import Order
from app.models.product import Product


# All order/flow slots on Conversation. Must be kept in sync with the model.
# Fields intentionally NOT reset: phone_number, channel, created_at, updated_at,
# is_sandbox, last_customer_language, taken_over_at, taken_over_note.
RESET_VALUES = {
    "current_stage": "greeting",
    "ai_enabled": True,
    "order_intent_score": 0,
    "customer_name": None,
    "delivery_address": None,
    "pending_order_quantity": None,
    "payment_method": None,
    "pending_product_sku": None,
    "selected_color": None,
    "selected_size": None,
    "selected_material": None,
    "interrupted_sku": None,
    "summary_shown": False,
    "browsed_skus": None,        # code should coalesce to [] when reading
    "last_followup_sku": None,
    "followup_sent_at": None,
    "escalation_count": 0,
    "last_escalation_at": None,
}


async def purge_orders(db, conv_id: int) -> None:
    """Delete test orders for conv_id and restore any deducted stock."""
    result = await db.execute(
        select(Order).where(Order.conversation_id == conv_id)
    )
    orders = result.scalars().all()

    if not orders:
        print("  No orders found for this conversation — nothing purged.")
        return

    restored: dict[int, int] = {}  # product_id → qty restored

    for order in orders:
        if order.stock_deducted and order.product_id is not None:
            product = await db.get(Product, order.product_id)
            if product is not None and product.stock is not None:
                product.stock += order.quantity
                restored[order.product_id] = (
                    restored.get(order.product_id, 0) + order.quantity
                )

    await db.execute(
        delete(Order).where(Order.conversation_id == conv_id)
    )

    print(f"  Purged {len(orders)} order(s) for conv_id={conv_id}.")
    if restored:
        for pid, qty in restored.items():
            print(f"  Restored stock: product_id={pid} +{qty} units.")
    else:
        print("  No stock restoration needed (no stock_deducted orders).")


async def reset(conv_id, phone, do_purge: bool):
    """Reset conversation slots and optionally purge test orders."""
    factory = _get_session_factory()
    async with factory() as db:
        if conv_id is not None:
            stmt = select(Conversation).where(Conversation.id == conv_id)
        else:
            stmt = select(Conversation).where(Conversation.phone_number == phone)

        conv = (await db.execute(stmt)).scalar_one_or_none()
        if conv is None:
            print(f"No conversation found (id={conv_id}, phone={phone}).")
            return

        if do_purge:
            await purge_orders(db, conv.id)

        for field, value in RESET_VALUES.items():
            if hasattr(conv, field):
                setattr(conv, field, value)
            else:
                print(f"  (skipped unknown column: {field})")

        await db.commit()
        print(
            f"\nReset conv id={conv.id} phone={conv.phone_number} -> "
            f"stage='{conv.current_stage}', ai_enabled={conv.ai_enabled}, "
            f"escalation_count={conv.escalation_count}, all order slots cleared."
        )


def main():
    p = argparse.ArgumentParser(description="Reset a test conversation.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--id", type=int, default=52, help="conversation id (default 52)")
    g.add_argument("--phone", type=str, help="phone_number instead of id")
    p.add_argument(
        "--no-purge-orders",
        dest="purge_orders",
        action="store_false",
        default=True,
        help="skip deleting test orders and restoring stock",
    )
    args = p.parse_args()

    if args.phone:
        asyncio.run(reset(None, args.phone, args.purge_orders))
    else:
        asyncio.run(reset(args.id, None, args.purge_orders))


if __name__ == "__main__":
    main()
