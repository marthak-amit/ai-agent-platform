"""
Tests for app/scheduler.py's _send_abandoned_intent_followups — the "close the
sale" nudge (order flow rule 6): draft order open, no payment yet, 6h+ idle
since the customer's last inbound message, guarded to Meta's 24h free-form
session window.

All DB and external-service calls are mocked so no real DB or API is needed.

Coverage:
  - 6h-idle trigger sends a nudge (order_collection stage)
  - idle < 6h is skipped
  - idle > 24h is skipped and flagged (event=nudge_needs_template), not sent
  - payment stage resends UPI instructions for the pending_payment order
  - Instagram channel uses instagram_service.send_dm, not WhatsApp
  - 7-day cooldown skips a recently-followed-up conversation
  - same-SKU repeat is skipped
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import Order


def _conv(
    conv_id: int = 1,
    client_id: int = 5,
    stage: str = "order_collection",
    sku: str = "PR100",
    channel: str = "whatsapp",
    last_followup_sku: str | None = None,
    followup_sent_at: datetime | None = None,
) -> Conversation:
    conv = Conversation()
    conv.id = conv_id
    conv.client_id = client_id
    conv.current_stage = stage
    conv.pending_product_sku = sku
    conv.ai_enabled = True
    conv.channel = channel
    conv.phone_number = "919900000001"
    conv.customer_name = "Test Customer"
    conv.last_customer_language = "english"
    conv.last_followup_sku = last_followup_sku
    conv.followup_sent_at = followup_sent_at
    return conv


def _client(client_id: int = 5, is_active: bool = True, instagram_account_id: str | None = None) -> Client:
    c = Client()
    c.id = client_id
    c.is_active = is_active
    c.upi_id = "test@upi"
    c.instagram_account_id = instagram_account_id
    return c


def _message(created_at: datetime, role: str = "user") -> Message:
    m = Message()
    m.role = role
    m.created_at = created_at
    return m


def _order(conv_id: int, status: str = "pending_payment") -> Order:
    o = Order()
    o.conversation_id = conv_id
    o.status = status
    o.order_number = "ORD-2026-0001"
    o.total_amount = 999.0
    return o


def _scalar_result(obj):
    """A db.execute() result whose .scalar_one_or_none() returns obj."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = obj
    return m


def _candidates_result(convs):
    """A db.execute() result whose .scalars().all() returns convs."""
    m = MagicMock()
    m.scalars.return_value.all.return_value = convs
    return m


@pytest.mark.asyncio
async def test_sends_nudge_when_6h_idle_order_collection(mock_db):
    """A draft order idle 7h (customer's last inbound message) triggers a send."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv()
    client = _client()
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=7)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
        _scalar_result(client),
    ]

    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.outbound.send_text",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.x"}]}),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_called_once()
    assert send_mock.call_args.args[0] == conv.phone_number
    assert conv.last_followup_sku == "PR100"
    assert conv.followup_sent_at is not None


@pytest.mark.asyncio
async def test_skips_when_idle_under_6h(mock_db):
    """Idle time under the 6h threshold does not trigger a send."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv()
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=2)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
    ]

    with patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skips_and_flags_when_idle_over_24h(mock_db, caplog):
    """Idle past 24h is outside Meta's free-form window — skip and flag, don't send."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv()
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=30)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
    ]

    with patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as send_mock, caplog.at_level("WARNING"):
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_not_called()
    assert "event=nudge_needs_template" in caplog.text
    assert f"conv={conv.id}" in caplog.text


@pytest.mark.asyncio
async def test_payment_stage_resends_upi_instructions(mock_db):
    """In the payment stage, the pending_payment order's UPI details are resent."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv(stage="payment")
    client = _client()
    order = _order(conv.id)
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=8)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
        _scalar_result(client),
        _scalar_result(order),
    ]

    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.outbound.send_text",
        new=AsyncMock(return_value={}),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_called_once()
    sent_text = send_mock.call_args.args[1]
    assert order.order_number in sent_text
    assert client.upi_id in sent_text


@pytest.mark.asyncio
async def test_payment_stage_skips_when_no_pending_order(mock_db):
    """Payment-stage conversation with no matching pending_payment order is skipped."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv(stage="payment")
    client = _client()
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=8)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
        _scalar_result(client),
        _scalar_result(None),
    ]

    with patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_instagram_channel_uses_send_dm(mock_db):
    """Instagram conversations send via instagram_service.send_dm, not WhatsApp."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv(channel="instagram")
    conv.phone_number = "17841400000000"  # IGSID
    client = _client(instagram_account_id="ig-business-123")
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=7)

    mock_db.execute.side_effect = [
        _candidates_result([conv]),
        _scalar_result(_message(last_inbound)),
        _scalar_result(client),
    ]

    with patch(
        "app.services.catalogue_service.find_product_by_sku",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.outbound.ig_send_dm",
        new=AsyncMock(return_value={}),
    ) as dm_mock, patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as wa_mock:
        await _send_abandoned_intent_followups(mock_db)

    dm_mock.assert_called_once()
    assert dm_mock.call_args.args[0] == "ig-business-123"
    assert dm_mock.call_args.args[1] == conv.phone_number
    wa_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_skips_recent_followup(mock_db):
    """A conversation followed-up within the last 7 days is skipped."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv(
        last_followup_sku="PR999",
        followup_sent_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    mock_db.execute.side_effect = [_candidates_result([conv])]

    with patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_same_sku_repeat_skipped(mock_db):
    """A conversation already followed up for the same SKU is skipped."""
    from app.scheduler import _send_abandoned_intent_followups

    conv = _conv(last_followup_sku="PR100")  # matches conv.pending_product_sku

    mock_db.execute.side_effect = [_candidates_result([conv])]

    with patch(
        "app.services.outbound.send_text",
        new=AsyncMock(),
    ) as send_mock:
        await _send_abandoned_intent_followups(mock_db)

    send_mock.assert_not_called()
