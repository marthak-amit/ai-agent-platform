"""
In-process realtime event hub for the dashboard (SSE).

Choice: Server-Sent Events backed by an in-process pub/sub, with a stateless
DB-derived polling endpoint (GET /events/poll) as the fallback. SSE was chosen
over websockets because the dashboard only needs server→client pushes, SSE
rides plain HTTP through Railway's proxy, and EventSource reconnects on its
own. The in-process hub is correct for a single Railway instance — the same
constraint CLAUDE.md documents for the in-process rate limiter; when scaling
past one instance, swap `publish()` to Redis pub/sub (the polling endpoint is
already instance-agnostic because it reads the database, so clients degrade
gracefully).

Events (all scoped to one client_id):
    new_message           {conversation_id, message}
    payment_submitted     {order_id, conversation_id, proof_id}
    payment_reviewed      {order_id, conversation_id, decision}
    conversation_updated  {conversation_id, bot_paused}
    payment_setup_required {}
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAX = 200
_subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(client_id: int) -> asyncio.Queue:
    """Register and return a new bounded event queue for one dashboard connection."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers[client_id].add(queue)
    return queue


def unsubscribe(client_id: int, queue: asyncio.Queue) -> None:
    """Drop a connection's queue (idempotent)."""
    _subscribers[client_id].discard(queue)
    if not _subscribers[client_id]:
        _subscribers.pop(client_id, None)


def publish(client_id: int | None, event_type: str, data: dict[str, Any]) -> None:
    """
    Fan an event out to every live dashboard connection of this client.

    Never raises and never blocks: a full queue (stalled client) drops the
    event for that connection only — the client recovers via /events/poll.
    """
    if client_id is None:
        return
    event = {"type": event_type, "data": data}
    for queue in list(_subscribers.get(client_id, ())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("realtime: queue full for client=%s, dropping %s", client_id, event_type)


def subscriber_count(client_id: int) -> int:
    """Number of live connections for a client (used by tests)."""
    return len(_subscribers.get(client_id, ()))
