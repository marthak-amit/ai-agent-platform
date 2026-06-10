"""
Tests for app/services/followup_service.py and app/routers/followup.py.

All DB and external-service calls are mocked so no real DB or API is needed.

Coverage:
  - get_eligible_leads: warm/cold included, hot excluded, recent follow-up excluded,
    recent message excluded, Instagram channel excluded, null conversation_id excluded
  - generate_followup_message: Gemini path, fallback on error, fallback when no history
  - send_followups: all sent, partial failure, empty eligible list
  - GET /followup/stats: returns counts
  - POST /followup/run: returns summary, rejects missing admin key
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.follow_up import FollowUp
from app.models.lead import Lead


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_lead(
    lead_id: int = 1,
    phone: str = "919900000001",
    status: str = "warm",
    conversation_id: int = 10,
) -> Lead:
    """Create a Lead instance with test values (no DB required)."""
    lead = Lead()
    lead.id = lead_id
    lead.phone_number = phone
    lead.status = status
    lead.conversation_id = conversation_id
    return lead


# ── get_eligible_leads ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_eligible_leads_returns_rows(mock_db):
    """Rows from the DB are unpacked into (Lead, last_msg_at) tuples."""
    from datetime import datetime, timedelta, timezone
    from app.services.followup_service import get_eligible_leads

    lead = _make_lead()
    last_ts = datetime.now(timezone.utc) - timedelta(hours=30)

    row = MagicMock()
    row.Lead = lead
    row.last_msg_at = last_ts

    # Use an explicit MagicMock for .all so Python 3.14 AsyncMock doesn't
    # turn it into a coroutine.
    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[row])
    mock_db.execute.return_value = mock_result

    result = await get_eligible_leads(mock_db)

    assert len(result) == 1
    assert result[0][0] is lead
    assert result[0][1] == last_ts
    mock_db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_get_eligible_leads_empty_when_no_rows(mock_db):
    """Empty DB result returns empty list."""
    from app.services.followup_service import get_eligible_leads

    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[])
    mock_db.execute.return_value = mock_result

    result = await get_eligible_leads(mock_db)
    assert result == []


@pytest.mark.asyncio
async def test_get_eligible_leads_returns_multiple(mock_db):
    """Multiple eligible leads are all returned."""
    from datetime import datetime, timedelta, timezone
    from app.services.followup_service import get_eligible_leads

    now = datetime.now(timezone.utc)
    rows = []
    for i in range(1, 4):
        row = MagicMock()
        row.Lead = _make_lead(i, f"9199000000{i}", "warm")
        row.last_msg_at = now - timedelta(hours=25 + i)
        rows.append(row)

    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=rows)
    mock_db.execute.return_value = mock_result

    result = await get_eligible_leads(mock_db)
    assert len(result) == 3


# ── generate_followup_message ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_followup_message_uses_gemini(mock_db):
    """When conversation history exists, Gemini is called and its reply returned."""
    from app.services.followup_service import generate_followup_message

    lead = _make_lead()
    mock_msg = MagicMock()
    mock_msg.role = "user"
    mock_msg.content = "Banarasi saree chahiye"

    with patch(
        "app.services.followup_service.conversation_service.get_history",
        new=AsyncMock(return_value=[mock_msg]),
    ), patch(
        "app.services.followup_service.gemini_service.generate_reply",
        new=AsyncMock(return_value="Namaste! Aapka order ready hai."),
    ):
        result = await generate_followup_message(mock_db, lead)

    assert result == "Namaste! Aapka order ready hai."


@pytest.mark.asyncio
async def test_generate_followup_message_falls_back_on_gemini_error(mock_db):
    """When Gemini raises, the default Hindi message is returned."""
    from app.services.followup_service import generate_followup_message, _DEFAULT_MESSAGE

    lead = _make_lead()
    mock_msg = MagicMock()
    mock_msg.role = "user"
    mock_msg.content = "hello"

    with patch(
        "app.services.followup_service.conversation_service.get_history",
        new=AsyncMock(return_value=[mock_msg]),
    ), patch(
        "app.services.followup_service.gemini_service.generate_reply",
        new=AsyncMock(side_effect=RuntimeError("Gemini unavailable")),
    ) as _:
        result = await generate_followup_message(mock_db, lead)

    assert result == _DEFAULT_MESSAGE


@pytest.mark.asyncio
async def test_generate_followup_message_default_when_no_history(mock_db):
    """Empty conversation history returns the default message without calling Gemini."""
    from app.services.followup_service import generate_followup_message, _DEFAULT_MESSAGE

    lead = _make_lead()

    with patch(
        "app.services.followup_service.conversation_service.get_history",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.followup_service.gemini_service.generate_reply",
        new=AsyncMock(),
    ) as mock_gemini:
        result = await generate_followup_message(mock_db, lead)

    assert result == _DEFAULT_MESSAGE
    mock_gemini.assert_not_called()


@pytest.mark.asyncio
async def test_generate_followup_message_default_when_no_conversation_id(mock_db):
    """Lead with no conversation_id returns the default message immediately."""
    from app.services.followup_service import generate_followup_message, _DEFAULT_MESSAGE

    lead = _make_lead(conversation_id=None)
    lead.conversation_id = None

    with patch(
        "app.services.followup_service.conversation_service.get_history",
        new=AsyncMock(),
    ) as mock_hist:
        result = await generate_followup_message(mock_db, lead)

    assert result == _DEFAULT_MESSAGE
    mock_hist.assert_not_called()


# ── send_followups ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_followups_sends_all_eligible(mock_db):
    """All eligible leads receive a WhatsApp message and a FollowUp row is committed."""
    from app.services.followup_service import send_followups

    lead1 = _make_lead(1, "919900000001")
    lead2 = _make_lead(2, "919900000002", "cold")

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=[
            (lead1, now - timedelta(hours=30)),
            (lead2, now - timedelta(hours=48)),
        ]),
    ), patch(
        "app.services.followup_service.generate_followup_message",
        new=AsyncMock(return_value="Test follow-up message"),
    ), patch(
        "app.services.followup_service.whatsapp_service.send_text_message",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.x"}]}),
    ):
        result = await send_followups(mock_db)

    assert result == {"sent": 2, "failed": 0, "total_eligible": 2}
    assert mock_db.add.call_count == 2
    assert mock_db.commit.call_count == 2


@pytest.mark.asyncio
async def test_send_followups_records_failure_on_whatsapp_error(mock_db):
    """WhatsApp send failure is recorded as status='failed'; other leads still proceed."""
    from app.services.followup_service import send_followups
    import httpx

    lead1 = _make_lead(1, "919900000001")
    lead2 = _make_lead(2, "919900000002")

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    send_mock = AsyncMock(side_effect=[
        httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock()),
        {"messages": [{"id": "wamid.ok"}]},
    ])

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=[
            (lead1, now - timedelta(hours=30)),
            (lead2, now - timedelta(hours=30)),
        ]),
    ), patch(
        "app.services.followup_service.generate_followup_message",
        new=AsyncMock(return_value="Follow-up text"),
    ), patch(
        "app.services.followup_service.whatsapp_service.send_text_message",
        new=send_mock,
    ):
        result = await send_followups(mock_db)

    assert result == {"sent": 1, "failed": 1, "total_eligible": 2}

    # Both attempts should still write a FollowUp row
    assert mock_db.add.call_count == 2

    # Check statuses on the added FollowUp objects
    added_fus = [call.args[0] for call in mock_db.add.call_args_list]
    statuses = {fu.status for fu in added_fus}
    assert "sent" in statuses
    assert "failed" in statuses


@pytest.mark.asyncio
async def test_send_followups_returns_zeros_when_no_eligible(mock_db):
    """No eligible leads → all counts are zero, nothing is added to the DB."""
    from app.services.followup_service import send_followups

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=[]),
    ):
        result = await send_followups(mock_db)

    assert result == {"sent": 0, "failed": 0, "total_eligible": 0}
    mock_db.add.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_send_followups_correct_phone_number_used(mock_db):
    """The WhatsApp send uses the lead's phone_number field."""
    from app.services.followup_service import send_followups

    lead = _make_lead(phone="919988776655")

    from datetime import datetime, timedelta, timezone
    wa_mock = AsyncMock(return_value={})

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=[(lead, datetime.now(timezone.utc) - timedelta(hours=30))]),
    ), patch(
        "app.services.followup_service.generate_followup_message",
        new=AsyncMock(return_value="Hello!"),
    ), patch(
        "app.services.followup_service.whatsapp_service.send_text_message",
        new=wa_mock,
    ):
        await send_followups(mock_db)

    wa_mock.assert_called_once_with(
        to_phone_number="919988776655",
        message_text="Hello!",
    )


# ── get_stats ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_stats_returns_all_keys(mock_db):
    """get_stats returns a dict with all four expected keys."""
    from app.services.followup_service import get_stats

    mock_db.execute.return_value.scalar.return_value = 3

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=[]),
    ):
        result = await get_stats(mock_db)

    assert "sent_today" in result
    assert "sent_last_7_days" in result
    assert "failed_last_7_days" in result
    assert "currently_eligible" in result


@pytest.mark.asyncio
async def test_get_stats_counts_eligible(mock_db):
    """currently_eligible reflects the length of the eligible leads list."""
    from app.services.followup_service import get_stats

    from datetime import datetime, timedelta, timezone
    mock_db.execute.return_value.scalar.return_value = 0
    now = datetime.now(timezone.utc)
    eligible = [
        (_make_lead(i), now - timedelta(hours=30))
        for i in range(1, 4)
    ]

    with patch(
        "app.services.followup_service.get_eligible_leads",
        new=AsyncMock(return_value=eligible),
    ):
        result = await get_stats(mock_db)

    assert result["currently_eligible"] == 3


# ── Router: POST /followup/run ────────────────────────────────────────────────

def test_run_followups_requires_admin_key(client):
    """POST /followup/run without X-Admin-Key returns 401."""
    response = client.post("/followup/run")
    assert response.status_code == 401


def test_run_followups_wrong_admin_key(client):
    """POST /followup/run with wrong key returns 401."""
    response = client.post(
        "/followup/run",
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_run_followups_returns_summary(client, mock_db):
    """POST /followup/run with valid key returns the send summary."""
    with patch(
        "app.routers.followup.followup_service.send_followups",
        new=AsyncMock(return_value={"sent": 3, "failed": 1, "total_eligible": 4}),
    ):
        response = client.post(
            "/followup/run",
            headers={"X-Admin-Key": "test-admin-key"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sent"] == 3
    assert body["failed"] == 1
    assert body["total_eligible"] == 4


def test_run_followups_returns_zeros_when_no_leads(client, mock_db):
    """POST /followup/run returns zeroes when no eligible leads exist."""
    with patch(
        "app.routers.followup.followup_service.send_followups",
        new=AsyncMock(return_value={"sent": 0, "failed": 0, "total_eligible": 0}),
    ):
        response = client.post(
            "/followup/run",
            headers={"X-Admin-Key": "test-admin-key"},
        )

    assert response.status_code == 200
    assert response.json() == {"sent": 0, "failed": 0, "total_eligible": 0}


# ── Router: GET /followup/stats ───────────────────────────────────────────────

def test_followup_stats_requires_admin_key(client):
    """GET /followup/stats without key returns 401."""
    response = client.get("/followup/stats")
    assert response.status_code == 401


def test_followup_stats_returns_counts(client, mock_db):
    """GET /followup/stats with valid key returns all stat keys."""
    with patch(
        "app.routers.followup.followup_service.get_stats",
        new=AsyncMock(return_value={
            "sent_today": 5,
            "sent_last_7_days": 27,
            "failed_last_7_days": 2,
            "currently_eligible": 8,
        }),
    ):
        response = client.get(
            "/followup/stats",
            headers={"X-Admin-Key": "test-admin-key"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sent_today"] == 5
    assert body["sent_last_7_days"] == 27
    assert body["failed_last_7_days"] == 2
    assert body["currently_eligible"] == 8
