"""
Tests for app/services/usage_service.py and app/routers/usage.py.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.client import Client
from app.models.usage_log import UsageLog
from app.services import usage_service


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_client(**kwargs) -> Client:
    """Build a Client with sensible defaults."""
    defaults = dict(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, daily_message_limit=100, whatsapp_number="+919876543210",
    )
    defaults.update(kwargs)
    return Client(**defaults)


def _make_db(existing_log: UsageLog | None = None, monthly_sum: int = 0) -> AsyncMock:
    """Return an AsyncMock DB that returns existing_log on the first execute call."""
    db = AsyncMock()

    today_result = MagicMock()
    today_result.scalar_one_or_none.return_value = existing_log

    monthly_result = MagicMock()
    monthly_result.scalar.return_value = monthly_sum

    # First call → today log; second call → monthly sum
    db.execute = AsyncMock(side_effect=[today_result, monthly_result])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


# ── usage_service.record_message ─────────────────────────────────────────────

async def test_record_message_creates_new_log():
    """First message of the day creates a UsageLog with count=1."""
    client = _make_client()
    db = AsyncMock()
    no_log = MagicMock()
    no_log.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=no_log)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)
    db.add = MagicMock()

    log = await usage_service.record_message(db, client)

    db.add.assert_called_once()
    db.commit.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.message_count == 1
    assert added.client_id == 1


async def test_record_message_increments_existing_log():
    """Subsequent messages increment the existing log's count."""
    client = _make_client()
    existing = UsageLog(id=1, client_id=1, date=date.today(), message_count=9)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)
    db.add = MagicMock()

    await usage_service.record_message(db, client)

    assert existing.message_count == 10
    db.add.assert_not_called()


async def test_record_message_sends_80_percent_warning():
    """80% threshold sends a WhatsApp warning exactly once (boundary crossing)."""
    # Limit 100, threshold 80; old_count=79 → new will be 80 → crosses boundary
    client = _make_client(daily_message_limit=100, whatsapp_number="+919876543210")
    existing = UsageLog(id=1, client_id=1, date=date.today(), message_count=79)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    def _set_count(obj):
        # simulate DB refresh updating the count
        pass

    db.refresh = AsyncMock(side_effect=_set_count)
    db.add = MagicMock()

    with patch(
        "app.services.whatsapp_service.send_text_message",
        new_callable=AsyncMock,
    ) as mock_send:
        await usage_service.record_message(db, client)

    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert call_args.kwargs["to_phone_number"] == "+919876543210"
    assert "80" in call_args.kwargs["message_text"] or "Usage Alert" in call_args.kwargs["message_text"]


async def test_record_message_does_not_resend_80_warning():
    """80% warning is NOT re-sent when count is already above threshold."""
    client = _make_client(daily_message_limit=100)
    # old_count=85 → new=86; threshold=80 already crossed previously
    existing = UsageLog(id=1, client_id=1, date=date.today(), message_count=85)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)
    db.add = MagicMock()

    with patch(
        "app.services.whatsapp_service.send_text_message",
        new_callable=AsyncMock,
    ) as mock_send:
        await usage_service.record_message(db, client)

    mock_send.assert_not_called()


async def test_record_message_no_warning_when_no_whatsapp_number():
    """No WhatsApp warning if client has no whatsapp_number set."""
    client = _make_client(daily_message_limit=100, whatsapp_number=None)
    existing = UsageLog(id=1, client_id=1, date=date.today(), message_count=79)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)
    db.add = MagicMock()

    with patch(
        "app.services.whatsapp_service.send_text_message",
        new_callable=AsyncMock,
    ) as mock_send:
        await usage_service.record_message(db, client)

    mock_send.assert_not_called()


async def test_record_message_logs_at_100_percent():
    """At daily limit, a warning is logged (no exception raised)."""
    client = _make_client(daily_message_limit=10)
    existing = UsageLog(id=1, client_id=1, date=date.today(), message_count=9)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)
    db.add = MagicMock()

    with patch(
        "app.services.whatsapp_service.send_text_message",
        new_callable=AsyncMock,
    ):
        log = await usage_service.record_message(db, client)

    # count reaches 10 == limit; function returns normally (soft limit)
    assert existing.message_count == 10


# ── usage_service.get_stats ───────────────────────────────────────────────────

async def test_get_stats_with_no_messages():
    """Client with no messages today returns zeros."""
    client = _make_client(daily_message_limit=100)
    db = _make_db(existing_log=None, monthly_sum=0)

    stats = await usage_service.get_stats(db, client)

    assert stats["today_count"] == 0
    assert stats["monthly_count"] == 0
    assert stats["limit"] == 100
    assert stats["percentage_used"] == 0.0


async def test_get_stats_with_existing_messages():
    """Client with 50 messages shows 50% usage."""
    client = _make_client(daily_message_limit=100)
    today_log = UsageLog(id=1, client_id=1, date=date.today(), message_count=50)
    db = _make_db(existing_log=today_log, monthly_sum=350)

    stats = await usage_service.get_stats(db, client)

    assert stats["today_count"] == 50
    assert stats["monthly_count"] == 350
    assert stats["limit"] == 100
    assert stats["percentage_used"] == 50.0


async def test_get_stats_percentage_rounds_to_one_decimal():
    """percentage_used is rounded to 1 decimal place."""
    client = _make_client(daily_message_limit=300)
    today_log = UsageLog(id=1, client_id=1, date=date.today(), message_count=100)
    db = _make_db(existing_log=today_log, monthly_sum=100)

    stats = await usage_service.get_stats(db, client)

    assert stats["percentage_used"] == round(100 / 300 * 100, 1)


# ── usage router tests ────────────────────────────────────────────────────────

def test_get_usage_stats_returns_200(client, mock_db, mock_settings):
    """GET /usage/stats returns stats for the authenticated client."""
    from app.services.auth_service import create_access_token

    token = create_access_token({"sub": "owner@biz.com"})
    existing_client = Client(
        id=1, email="owner@biz.com", hashed_password="h",
        is_active=True, daily_message_limit=100,
    )
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = existing_client
    mock_db.execute.return_value = auth_result

    fake_stats = {
        "today_count": 42,
        "monthly_count": 800,
        "limit": 100,
        "percentage_used": 42.0,
    }
    with patch(
        "app.services.usage_service.get_stats",
        new=AsyncMock(return_value=fake_stats),
    ):
        response = client.get(
            "/usage/stats",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["today_count"] == 42
    assert data["monthly_count"] == 800
    assert data["limit"] == 100
    assert data["percentage_used"] == 42.0


def test_get_usage_stats_requires_auth(client):
    """GET /usage/stats returns 401 without a token."""
    response = client.get("/usage/stats")
    assert response.status_code == 401
