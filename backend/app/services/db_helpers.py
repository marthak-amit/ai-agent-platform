"""Small database helpers shared by services."""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def add_or_get(db: AsyncSession, obj: T, lookup) -> tuple[T, bool]:
    """
    INSERT `obj`, or return the row a concurrent request inserted first.

    The usual "SELECT; if none: INSERT" races when two requests carry the same unique key (a customer's first
    two messages, two conversations opening a billing period, ...): the loser hit the unique index and its whole
    webhook request failed. The INSERT runs inside a SAVEPOINT, so losing the race rolls back only the insert;
    `lookup` (a select statement) then fetches the winner's row.

    Returns:
        (row, created) — created is False when an existing row was returned instead of `obj`.
    """
    try:
        async with db.begin_nested():
            db.add(obj)
            await db.flush()
        return obj, True
    except IntegrityError:
        existing = (await db.execute(lookup.execution_options(populate_existing=True))).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
