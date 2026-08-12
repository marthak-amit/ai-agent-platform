"""Tests for app/services/billing_service.py."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.client import Client
from app.models.client_monthly_usage import ClientMonthlyUsage
from app.models.conversation import Conversation
from app.services import billing_service


def _make_client(**overrides) -> Client:
    defaults = dict(
        id=1, email="x@y.com", hashed_password="h", plan_slug="growth",
        plan_conv_limit_snapshot=2000, plan_price_snapshot=3999,
        plan_image_quota_snapshot=50, plan_image_overage_price_snapshot=6,
        billing_cycle_start=date.today(), plan_grandfathered=False,
        conv_limit_warned_period=None, whatsapp_number=None,
    )
    defaults.update(overrides)
    return Client(**defaults)


# ── ensure_current_cycle ──────────────────────────────────────────────────────

async def test_ensure_current_cycle_sets_start_when_missing(mock_db, seeded_plans):
    """A client with no billing_cycle_start gets one set to today."""
    client = _make_client(billing_cycle_start=None)

    await billing_service.ensure_current_cycle(mock_db, client)

    assert client.billing_cycle_start == date.today()
    mock_db.commit.assert_called()


async def test_ensure_current_cycle_rolls_forward_when_elapsed(mock_db, seeded_plans):
    """A client whose cycle elapsed >1 month ago gets snapshot refreshed from the live plan."""
    old_start = date.today() - timedelta(days=45)
    client = _make_client(
        plan_slug="pro", billing_cycle_start=old_start,
        plan_conv_limit_snapshot=999, conv_limit_warned_period="2020-01",
    )

    await billing_service.ensure_current_cycle(mock_db, client)

    assert client.plan_conv_limit_snapshot == 6000  # pro's live conv_limit
    assert client.plan_price_snapshot == 9999
    assert client.billing_cycle_start > old_start
    assert client.conv_limit_warned_period is None


async def test_ensure_current_cycle_noop_when_not_elapsed(mock_db, seeded_plans):
    """A client mid-cycle is left untouched."""
    client = _make_client(billing_cycle_start=date.today() - timedelta(days=5))
    original_snapshot = client.plan_conv_limit_snapshot

    await billing_service.ensure_current_cycle(mock_db, client)

    assert client.plan_conv_limit_snapshot == original_snapshot


async def test_ensure_current_cycle_grandfathered_never_refreshes(mock_db, seeded_plans):
    """A grandfathered client's snapshot never refreshes even after the cycle elapses."""
    old_start = date.today() - timedelta(days=45)
    client = _make_client(
        plan_slug="pro", billing_cycle_start=old_start,
        plan_conv_limit_snapshot=999, plan_grandfathered=True,
    )

    await billing_service.ensure_current_cycle(mock_db, client)

    assert client.plan_conv_limit_snapshot == 999
    assert client.billing_cycle_start == old_start


# ── record_conversation_activity ──────────────────────────────────────────────

async def test_record_conversation_activity_creates_new_monthly_usage_row(mock_db, seeded_plans):
    """First activity this period creates a ClientMonthlyUsage row with conv_count=1."""
    client = _make_client()
    conv = Conversation(id=1, client_id=1, phone_number="91900000", usage_counted_period=None)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    await billing_service.record_conversation_activity(mock_db, client, conv)

    assert conv.usage_counted_period == billing_service._current_period()
    mock_db.add.assert_called_once()
    added = mock_db.add.call_args[0][0]
    assert isinstance(added, ClientMonthlyUsage)
    assert added.conv_count == 1


async def test_record_conversation_activity_dedupes_same_period(mock_db, seeded_plans):
    """A conversation already counted this period does not increment again."""
    period = billing_service._current_period()
    client = _make_client()
    conv = Conversation(id=1, client_id=1, phone_number="91900000", usage_counted_period=period)
    existing_usage = ClientMonthlyUsage(client_id=1, period=period, conv_count=5)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_usage
    mock_db.execute.return_value = mock_result

    await billing_service.record_conversation_activity(mock_db, client, conv)

    mock_db.add.assert_not_called()
    assert existing_usage.conv_count == 5


async def test_record_conversation_activity_sends_80pct_nudge_once(mock_db, seeded_plans):
    """Crossing the 80% conv_limit threshold sends one WhatsApp nudge, deduped by period."""
    period = billing_service._current_period()
    client = _make_client(plan_conv_limit_snapshot=10, whatsapp_number="919999999999")
    conv = Conversation(id=1, client_id=1, phone_number="91900000", usage_counted_period=period)
    existing_usage = ClientMonthlyUsage(client_id=1, period=period, conv_count=8)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_usage
    mock_db.execute.return_value = mock_result

    with patch(
        "app.services.whatsapp_service._raw_send_text_message", new=AsyncMock()
    ) as mock_send:
        await billing_service.record_conversation_activity(mock_db, client, conv)

    mock_send.assert_called_once()
    assert client.conv_limit_warned_period == period


async def test_record_conversation_activity_nudge_not_resent(mock_db, seeded_plans):
    """The 80% nudge is not resent once conv_limit_warned_period already matches."""
    period = billing_service._current_period()
    client = _make_client(
        plan_conv_limit_snapshot=10, whatsapp_number="919999999999",
        conv_limit_warned_period=period,
    )
    conv = Conversation(id=1, client_id=1, phone_number="91900000", usage_counted_period=period)
    existing_usage = ClientMonthlyUsage(client_id=1, period=period, conv_count=9)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_usage
    mock_db.execute.return_value = mock_result

    with patch(
        "app.services.whatsapp_service._raw_send_text_message", new=AsyncMock()
    ) as mock_send:
        await billing_service.record_conversation_activity(mock_db, client, conv)

    mock_send.assert_not_called()


# ── check_image_quota_and_bill_overage ────────────────────────────────────────

async def test_check_image_quota_flags_overage_at_quota(mock_db, seeded_plans):
    """Once the monthly count reaches the quota, the next image is an overage."""
    client = _make_client(plan_image_quota_snapshot=20, plan_image_overage_price_snapshot=6)

    client_result = MagicMock()
    client_result.scalar_one_or_none.return_value = client
    count_result = MagicMock()
    count_result.scalar_one.return_value = 20
    mock_db.execute.side_effect = [client_result, count_result]

    is_overage, overage_price = await billing_service.check_image_quota_and_bill_overage(mock_db, 1)

    assert is_overage is True
    assert overage_price == 6


async def test_check_image_quota_no_overage_under_quota(mock_db, seeded_plans):
    """Under the monthly quota, no overage is flagged."""
    client = _make_client(plan_image_quota_snapshot=20, plan_image_overage_price_snapshot=6)

    client_result = MagicMock()
    client_result.scalar_one_or_none.return_value = client
    count_result = MagicMock()
    count_result.scalar_one.return_value = 5
    mock_db.execute.side_effect = [client_result, count_result]

    is_overage, overage_price = await billing_service.check_image_quota_and_bill_overage(mock_db, 1)

    assert is_overage is False
    assert overage_price is None


async def test_check_image_quota_unknown_client_returns_false(mock_db, seeded_plans):
    """An unresolvable client_id returns (False, None) rather than raising."""
    client_result = MagicMock()
    client_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = client_result

    is_overage, overage_price = await billing_service.check_image_quota_and_bill_overage(mock_db, 999)

    assert is_overage is False
    assert overage_price is None
