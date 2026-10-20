"""
One-off: fill ``llm_usage.cost_usd`` (= cost_inr / USD_INR) on the rows written before migration 0073.

    cd backend && python scripts/backfill_llm_cost_usd.py --dry-run     # count only
    cd backend && python scripts/backfill_llm_cost_usd.py               # update, print how many rows

USD_INR is read from the environment / .env like the app (it is required). Only rows where cost_usd IS NULL are touched,
so running it twice is harmless; the ₹ column is never changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import _get_session_factory  # noqa: E402
from app.services import llm_usage_service  # noqa: E402


async def main(dry_run: bool) -> int:
    """Run the backfill (or count what it would do) and print the result."""
    async with _get_session_factory()() as db:
        count = await llm_usage_service.backfill_cost_usd(db, dry_run=dry_run)
    verb = "would update" if dry_run else "updated"
    print(f"{verb} {count} llm_usage row(s): cost_usd = cost_inr / {get_settings().usd_inr}")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only count the rows that would be updated")
    asyncio.run(main(parser.parse_args().dry_run))
