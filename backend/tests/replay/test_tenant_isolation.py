"""
Tenant isolation at the WhatsApp webhook door, against real Postgres.

An unknown / inactive / ambiguous / absent phone_number_id must be dropped:
no conversation, message, customer or outbound send is created for ANY tenant.
A known id must still reach only its own tenant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.services import whatsapp_service
from tests.replay.conftest import seed_client_and_product
from tests.replay.helpers import send_message, wa_text_payload

pytestmark = pytest.mark.asyncio


async def _row_counts(session) -> dict[str, int]:
    """Row counts of every table an accepted inbound message would write to."""
    from app.models.customer import Customer

    counts = {}
    for name, model in (("conversations", Conversation), ("messages", Message), ("customers", Customer)):
        counts[name] = (await session.execute(select(func.count()).select_from(model))).scalar_one()
    return counts


async def _seed_two_tenants(session):
    """Tenant A (id 1) and tenant B, each with their own phone_number_id."""
    a, _ = await seed_client_and_product(
        session, phone="919000000001", wa_phone_number_id="PNID_A", product_sku="A001"
    )
    b, _ = await seed_client_and_product(
        session, phone="919000000002", wa_phone_number_id="PNID_B", product_sku="B001"
    )
    return a, b


async def test_unknown_phone_number_id_is_dropped_and_creates_nothing(replay_http, replay_session):
    await _seed_two_tenants(replay_session)

    resp = await send_message(replay_http, "919111111111", "hi", phone_number_id="PNID_UNKNOWN")

    assert resp.status_code == 200
    assert resp.json() == {"status": "unmapped_number"}
    assert await _row_counts(replay_session) == {"conversations": 0, "messages": 0, "customers": 0}
    whatsapp_service._raw_send_text_message.assert_not_called()
    whatsapp_service._raw_send_typing_indicator.assert_not_called()


async def test_missing_phone_number_id_is_dropped(replay_http, replay_session):
    await _seed_two_tenants(replay_session)
    body, headers = wa_text_payload("919111111112", "hi")
    # Strip the metadata id from the signed payload and re-sign.
    import json

    from tests.replay.helpers import _sign

    payload = json.loads(body)
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = ""
    body = json.dumps(payload).encode()

    resp = await replay_http.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}
    )

    assert resp.json() == {"status": "unmapped_number"}
    assert await _row_counts(replay_session) == {"conversations": 0, "messages": 0, "customers": 0}
    whatsapp_service._raw_send_text_message.assert_not_called()


async def test_inactive_tenant_number_is_dropped_not_rerouted(replay_http, replay_session):
    a, b = await _seed_two_tenants(replay_session)
    b.is_active = False
    await replay_session.commit()

    resp = await send_message(replay_http, "919111111113", "hi", phone_number_id="PNID_B")

    assert resp.json() == {"status": "unmapped_number"}
    # Crucially: not answered on behalf of the still-active tenant A either.
    assert await _row_counts(replay_session) == {"conversations": 0, "messages": 0, "customers": 0}
    whatsapp_service._raw_send_text_message.assert_not_called()


async def test_phone_number_id_shared_by_two_active_tenants_is_dropped(replay_http, replay_session):
    a, b = await _seed_two_tenants(replay_session)
    b.whatsapp_phone_number_id = "PNID_A"  # duplicate mapping (no DB unique constraint today)
    await replay_session.commit()

    resp = await send_message(replay_http, "919111111114", "hi", phone_number_id="PNID_A")

    assert resp.json() == {"status": "unmapped_number"}
    assert await _row_counts(replay_session) == {"conversations": 0, "messages": 0, "customers": 0}
    whatsapp_service._raw_send_text_message.assert_not_called()


async def test_known_phone_number_id_reaches_only_its_own_tenant(replay_http, replay_session):
    a, b = await _seed_two_tenants(replay_session)

    resp = await send_message(replay_http, "919111111115", "hi", phone_number_id="PNID_B")

    assert resp.status_code == 200
    assert resp.json() != {"status": "unmapped_number"}
    convs = (await replay_session.execute(select(Conversation))).scalars().all()
    assert [c.client_id for c in convs] == [b.id]
    assert a.id != b.id
    assert (await replay_session.execute(
        select(func.count()).select_from(Client).where(Client.is_active == True)  # noqa: E712
    )).scalar_one() == 2
