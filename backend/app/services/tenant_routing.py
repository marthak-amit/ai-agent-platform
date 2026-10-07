"""
Tenant resolution for inbound channel webhooks.

A webhook identifies the receiving business only by a channel id Meta puts in
the payload (WhatsApp ``phone_number_id``, Instagram business ``entry.id``).
Mapping that id to a ``Client`` row is a tenant-isolation boundary: getting it
wrong hands one tenant's customers, messages and replies to another tenant.

Rules enforced here (shared by every webhook router so they cannot drift):

* absent / unknown / inactive id  -> ``None`` (caller drops the event)
* id mapped to MORE than one active client -> ``None`` + ERROR log (fail closed;
  never pick "one of them" arbitrarily)
* there is NO "first active client" fallback, ever
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models.client import Client

logger = logging.getLogger(__name__)


async def find_active_client_by_channel_id(
    db: AsyncSession,
    column: InstrumentedAttribute,
    channel_id: str | None,
    *,
    label: str,
) -> Client | None:
    """
    Return the single active client whose ``column`` equals ``channel_id``.

    Args:
        db:         Active async DB session.
        column:     The Client column holding the channel id
                    (``Client.whatsapp_phone_number_id`` / ``Client.instagram_account_id``).
        channel_id: The id taken from the webhook payload. May be None/empty.
        label:      Human-readable id name for log lines (e.g. ``"phone_number_id"``).

    Returns:
        The matching active Client, or None when the id is missing, unmapped,
        inactive, or ambiguous (mapped to more than one active client).
        Callers must drop the event on None.
    """
    if not channel_id:
        logger.warning("Webhook without a %s — dropped (no tenant mapping).", label)
        return None

    # limit(2): enough to detect ambiguity without scanning the table.
    result = await db.execute(
        select(Client)
        .where(column == channel_id, Client.is_active == True)  # noqa: E712
        .order_by(Client.id)
        .limit(2)
    )
    clients = list(result.scalars().all())

    if not clients:
        logger.warning("No active client mapped to %s=%s — webhook dropped.", label, channel_id)
        return None
    if len(clients) > 1:
        logger.error(
            "%s=%s is mapped to multiple active clients (%s) — webhook dropped; "
            "fix the duplicate mapping.",
            label, channel_id, ", ".join(str(c.id) for c in clients),
        )
        return None
    return clients[0]
