"""
One-shot migration: normalize bad role values in the messages table.

Run from backend/:
    python scripts/fix_message_roles.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    async with engine.begin() as conn:
        # Fix AI/bot/model roles → assistant (Groq convention)
        r1 = await conn.execute(
            text(
                "UPDATE messages SET role = 'assistant' "
                "WHERE role IN ('model', 'agent', 'ai', 'bot', 'Agent', 'AI')"
            )
        )
        print(f"Rows fixed (bad assistant roles → 'assistant'): {r1.rowcount}")

        # Fix human/owner roles → user
        r2 = await conn.execute(
            text(
                "UPDATE messages SET role = 'user' "
                "WHERE role IN ('human', 'you', 'owner', 'Human', 'You', 'Owner')"
            )
        )
        print(f"Rows fixed (bad user roles → 'user'):        {r2.rowcount}")

        # Catch-all: anything still not in the valid set → user
        r3 = await conn.execute(
            text(
                "UPDATE messages SET role = 'user' "
                "WHERE role NOT IN ('user', 'assistant', 'system')"
            )
        )
        print(f"Rows fixed (catch-all unknown → 'user'):     {r3.rowcount}")

        # Report distinct roles remaining
        result = await conn.execute(
            text("SELECT DISTINCT role FROM messages ORDER BY role")
        )
        roles = [row[0] for row in result.fetchall()]
        print(f"\nDistinct role values after fix: {roles}")

        bad = [r for r in roles if r not in ("user", "assistant", "system")]
        if bad:
            print(f"WARNING: unexpected roles still present: {bad}")
        else:
            print("OK: only valid roles remain.")

    await engine.dispose()


asyncio.run(main())
