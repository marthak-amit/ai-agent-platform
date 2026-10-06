"""Tests for the one-time SSE stream ticket (replaces the JWT in /events/stream's query string)."""

import pytest
from fastapi import HTTPException

from app.routers import realtime
from app.services import realtime_service


def test_issue_and_redeem_ticket_is_single_use():
    """A ticket redeems once, then is rejected."""
    ticket = realtime_service.issue_ticket(42)
    assert realtime_service.redeem_ticket(ticket) == 42
    assert realtime_service.redeem_ticket(ticket) is None


def test_redeem_ticket_rejects_expired():
    """A ticket past its TTL is rejected."""
    ticket = realtime_service.issue_ticket(1)
    realtime_service._tickets[ticket] = (1, 0.0)
    assert realtime_service.redeem_ticket(ticket) is None


async def test_client_from_ticket_rejects_unknown():
    """The stream endpoint 401s on an unknown ticket."""
    with pytest.raises(HTTPException) as exc:
        await realtime._client_from_ticket("nope")
    assert exc.value.status_code == 401


async def test_create_stream_ticket_returns_redeemable_ticket():
    """POST /events/ticket mints a ticket bound to the caller."""
    class _C:
        id = 9
    out = await realtime.create_stream_ticket(client=_C())
    assert realtime_service.redeem_ticket(out["ticket"]) == 9
    assert out["expires_in"] == 60
