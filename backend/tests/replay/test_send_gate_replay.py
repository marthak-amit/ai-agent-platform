"""
Replay coverage for the centralized outbound send gate, against real Postgres.

Asserts the acceptance-critical paths end-to-end:
  * an opted-out customer inside a broadcast segment gets ZERO sends and the
    suppression is logged;
  * the follow-up engine is provably silent outside the 24h window (zero raw
    API calls) but sends inside it;
  * a fresh inbound message re-opens the window (pipeline reply allowed and
    customers.last_inbound_at is refreshed by the webhook);
  * a blocked customer suppresses the pipeline reply at the gate even if an
    upstream guard were bypassed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from sqlalchemy import select

from tests.replay.conftest import seed_client_and_product
from tests.replay.helpers import send_message

pytestmark = pytest.mark.asyncio


async def _seed_customer(
    session,
    client_id: int,
    phone: str,
    *,
    opted_out: bool = False,
    is_blocked: bool = False,
    last_inbound_hours_ago: float | None = 1.0,
):
    """Insert a Customer row in a known gate state."""
    from app.models.customer import Customer

    customer = Customer(
        client_id=client_id,
        phone=phone,
        opted_out=opted_out,
        is_blocked=is_blocked,
        last_inbound_at=(
            datetime.now(timezone.utc) - timedelta(hours=last_inbound_hours_ago)
            if last_inbound_hours_ago is not None
            else None
        ),
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)
    return customer


async def _seed_lead_with_inbound(session, client_id: int, phone: str, hours_ago: float):
    """Conversation + one inbound message aged *hours_ago* + a warm Lead."""
    from app.models.conversation import Conversation
    from app.models.lead import Lead
    from app.models.message import Message

    conv = Conversation(
        phone_number=phone, client_id=client_id, channel="whatsapp",
        current_stage="product_inquiry",
    )
    session.add(conv)
    await session.flush()

    msg = Message(conversation_id=conv.id, role="user", content="kitne ka hai?")
    session.add(msg)
    await session.flush()
    msg.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)

    lead = Lead(
        phone_number=phone, status="warm", conversation_id=conv.id,
        client_id=client_id,
    )
    session.add(lead)
    await session.commit()
    await session.refresh(lead)
    return conv, lead


async def test_broadcast_skips_opted_out_recipient_and_logs_deny(
    replay_session, caplog
):
    """Opted-out customer in a campaign segment: zero sends, DENY logged."""
    from app.models.campaign import Campaign
    from app.models.campaign_recipient import CampaignRecipient
    from app.services import campaign_service

    client, _ = await seed_client_and_product(replay_session)
    await _seed_customer(
        replay_session, client.id, "919900000111",
        opted_out=True, last_inbound_hours_ago=1,
    )

    campaign = Campaign(
        client_id=client.id,
        name="Diwali sale",
        message_template="Hi {name}! Big sale today.",
        status="draft",
        total_recipients=1,
    )
    replay_session.add(campaign)
    await replay_session.flush()
    replay_session.add(CampaignRecipient(
        campaign_id=campaign.id,
        phone_number="919900000111",
        status="pending",
    ))
    await replay_session.commit()

    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message", new=mock.AsyncMock()
    ) as raw:
        with caplog.at_level("WARNING"):
            await campaign_service.send_campaign(campaign.id, replay_session)

    raw.assert_not_called()
    assert any(
        "event=send_suppressed" in r.getMessage() and "opted_out" in r.getMessage()
        for r in caplog.records
    )

    await replay_session.refresh(campaign)
    assert campaign.sent_count == 0
    recipients = (await replay_session.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    )).scalars().all()
    assert [r.status for r in recipients] == ["suppressed"]


async def test_followup_engine_silent_outside_24h_window(replay_session):
    """last_inbound 25h ago → follow-up suppressed with zero outbound API calls."""
    from app.services import followup_service

    client, _ = await seed_client_and_product(replay_session)
    phone = "919900000222"
    await _seed_customer(replay_session, client.id, phone, last_inbound_hours_ago=25)
    _conv, lead = await _seed_lead_with_inbound(replay_session, client.id, phone, 25)

    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message", new=mock.AsyncMock()
    ) as raw, mock.patch(
        "app.services.followup_service.get_eligible_leads",
        new=mock.AsyncMock(return_value=[
            (lead, datetime.now(timezone.utc) - timedelta(hours=25)),
        ]),
    ), mock.patch(
        "app.services.followup_service.generate_followup_message",
        new=mock.AsyncMock(return_value="come back!"),
    ):
        result = await followup_service.send_followups(replay_session)

    raw.assert_not_called()
    assert result["sent"] == 0
    assert result["blocked_window"] >= 1


async def test_followup_engine_sends_inside_24h_window(replay_session):
    """last_inbound 23h ago → follow-up goes out (free-form still allowed)."""
    from app.services import followup_service

    client, _ = await seed_client_and_product(replay_session)
    phone = "919900000333"
    await _seed_customer(replay_session, client.id, phone, last_inbound_hours_ago=23)
    _conv, lead = await _seed_lead_with_inbound(replay_session, client.id, phone, 23)

    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        new=mock.AsyncMock(return_value={"messages": [{"id": "wamid.f"}]}),
    ) as raw, mock.patch(
        "app.services.followup_service.get_eligible_leads",
        new=mock.AsyncMock(return_value=[
            (lead, datetime.now(timezone.utc) - timedelta(hours=23)),
        ]),
    ), mock.patch(
        "app.services.followup_service.generate_followup_message",
        new=mock.AsyncMock(return_value="come back!"),
    ):
        result = await followup_service.send_followups(replay_session)

    raw.assert_called_once()
    assert raw.call_args.kwargs["to_phone_number"] == phone
    assert result["sent"] == 1


async def test_fresh_inbound_reopens_window_and_updates_last_inbound_at(
    replay_http, replay_session
):
    """
    A customer stale for 25h sends a new message: the pipeline reply goes out
    (fresh inbound = window open) and customers.last_inbound_at is refreshed.
    """
    from app.models.customer import Customer
    from app.services import whatsapp_service

    client, _ = await seed_client_and_product(replay_session)
    phone = "919900000444"
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    await _seed_customer(replay_session, client.id, phone, last_inbound_hours_ago=25)

    resp = await send_message(replay_http, phone, "hello, kya available hai?")
    assert resp.status_code == 200

    # The reply may be text, buttons, or a list — conftest patches all three
    # raw senders; any of them counts as "the customer was answered".
    sent_any = any(
        getattr(whatsapp_service, name).called
        for name in (
            "_raw_send_text_message",
            "_raw_send_button_message",
            "_raw_send_list_message",
        )
    )
    assert sent_any, "pipeline reply to a fresh inbound must be sent"

    row = (await replay_session.execute(
        select(Customer).where(
            Customer.client_id == client.id, Customer.phone == phone
        )
    )).scalar_one()
    assert row.last_inbound_at is not None
    assert row.last_inbound_at.replace(tzinfo=timezone.utc) > stale + timedelta(hours=1)


async def test_gate_suppresses_pipeline_reply_to_blocked_customer(replay_session):
    """
    Defense in depth: even called directly (as if an upstream guard were
    bypassed), outbound.send_text refuses a blocked customer's reply.
    """
    from app.services import outbound
    from app.services.send_gate import MessageKind

    client, _ = await seed_client_and_product(replay_session)
    phone = "919900000555"
    await _seed_customer(replay_session, client.id, phone, is_blocked=True)

    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message", new=mock.AsyncMock()
    ) as raw:
        result = await outbound.send_text(
            phone, "reply text",
            kind=MessageKind.PIPELINE_REPLY,
            db=replay_session, client_id=client.id,
        )
    assert result is None
    raw.assert_not_called()
