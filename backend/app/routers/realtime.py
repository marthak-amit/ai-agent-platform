"""
Realtime feed for the dashboard.

- GET /events/stream?token=<jwt>   Server-Sent Events (primary). EventSource
                                   can't send an Authorization header, so the
                                   JWT rides in the query string.
- GET /events/poll?since=<iso>     Stateless polling fallback derived from the
                                   database — works across instances and when
                                   SSE is blocked by a proxy.

See app/services/realtime_service.py for the event catalogue and the
SSE-vs-websocket-vs-polling decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import _get_session_factory, get_db
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.payment_proof import PaymentProof
from app.routers.auth import get_current_client
from app.services import auth_service, channel_sender, realtime_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events", tags=["realtime"])

_HEARTBEAT_SECONDS = 15
_POLL_LIMIT = 200


async def _client_from_token(token: str) -> Client:
    """Authenticate the SSE connection, releasing the DB session immediately."""
    async with _get_session_factory()() as db:
        try:
            return await auth_service.get_current_client(token, db)
        except ValueError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.get("/stream")
async def stream_events(request: Request, token: str = Query(...)) -> StreamingResponse:
    """Open the SSE stream for the caller's business (events: see realtime_service)."""
    client = await _client_from_token(token)
    queue = realtime_service.subscribe(client.id)

    async def gen():
        """Yield SSE frames until the browser disconnects."""
        try:
            yield "retry: 3000\n: connected\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'], default=str)}\n\n"
        finally:
            realtime_service.unsubscribe(client.id, queue)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/poll")
async def poll_events(
    since: Optional[datetime] = Query(None, description="ISO timestamp; defaults to the last 60 seconds."),
    client: Client = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return events newer than `since`, derived from stored rows.

    Pass the returned `server_time` as the next `since`. Only new_message
    (inbound), payment_submitted and payment_reviewed are reconstructible;
    conversation_updated / payment_setup_required are SSE-only.
    """
    now = datetime.now(timezone.utc)
    if since is None:
        since = now - timedelta(seconds=60)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    events: list[dict] = []
    msgs = (await db.execute(
        select(Message).join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.client_id == client.id, Message.created_at > since)
        .order_by(Message.created_at).limit(_POLL_LIMIT)
    )).scalars().all()
    for m in msgs:
        events.append({
            "type": "new_message", "at": m.created_at.isoformat(),
            "data": {"conversation_id": m.conversation_id, "message": channel_sender.message_to_dict(m)},
        })
    submitted = (await db.execute(
        select(PaymentProof).where(PaymentProof.client_id == client.id, PaymentProof.created_at > since)
        .order_by(PaymentProof.created_at).limit(_POLL_LIMIT)
    )).scalars().all()
    for p in submitted:
        events.append({
            "type": "payment_submitted", "at": p.created_at.isoformat(),
            "data": {"order_id": p.order_id, "conversation_id": p.conversation_id, "proof_id": p.id},
        })
    reviewed = (await db.execute(
        select(PaymentProof).where(PaymentProof.client_id == client.id, PaymentProof.reviewed_at > since)
        .order_by(PaymentProof.reviewed_at).limit(_POLL_LIMIT)
    )).scalars().all()
    for p in reviewed:
        events.append({
            "type": "payment_reviewed", "at": p.reviewed_at.isoformat(),
            "data": {"order_id": p.order_id, "conversation_id": p.conversation_id,
                     "decision": p.status, "reviewed_by": p.reviewed_by_name},
        })
    events.sort(key=lambda e: e["at"])
    return {"server_time": now.isoformat(), "events": events[:_POLL_LIMIT]}
