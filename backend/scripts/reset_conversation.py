"""
One-off script: reset a specific conversation's order state for clean re-testing.

Usage:
    cd backend
    python scripts/reset_conversation.py

Resets phone=917575092467 back to "greeting" stage with all order slots
and the pinned SKU cleared.  Prints a fresh DB query after commit to confirm
persistence (does not trust the in-memory ORM object).
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db import _get_session_factory
from app.models.conversation import Conversation


TARGET_PHONE = "917575092467"

_RESET_FIELDS = {
    "current_stage": "greeting",
    "pending_product_sku": None,
    "pending_order_quantity": None,
    "selected_color": None,
    "selected_size": None,
    "selected_material": None,
    "customer_name": None,
    "delivery_address": None,
    "payment_method": None,
    "summary_shown": False,
}


async def main() -> None:
    """Reset the target conversation's order slots and stage."""
    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Conversation).where(
                Conversation.phone_number == TARGET_PHONE,
            ).limit(1)
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            print(f"No conversation found for phone={TARGET_PHONE}")
            return

        print(f"BEFORE — conversation id={conv.id}")
        for field in _RESET_FIELDS:
            print(f"  {field}={getattr(conv, field, '<missing>')!r}")

        for field, value in _RESET_FIELDS.items():
            if hasattr(conv, field):
                setattr(conv, field, value)
            else:
                print(f"  WARNING: field {field!r} not found on model — skipping")

        await db.commit()

        # Re-query to confirm persistence — never trust the in-memory object.
        result2 = await db.execute(
            select(Conversation).where(Conversation.id == conv.id).limit(1)
        )
        conv2 = result2.scalar_one_or_none()
        print(f"\nAFTER (fresh DB query) — conversation id={conv2.id}")
        for field in _RESET_FIELDS:
            val = getattr(conv2, field, "<missing>")
            ok = "✅" if val == _RESET_FIELDS[field] else "❌ MISMATCH"
            print(f"  {field}={val!r}  {ok}")

        print("\nReset complete. Re-send 'SR29821' to start a fresh order_collection flow.")


if __name__ == "__main__":
    asyncio.run(main())
