"""
Tenant-isolation tests for inbound channel webhooks (unit level, no database).

An unknown / unmapped / ambiguous WhatsApp phone_number_id or Instagram account
id must be logged and dropped — never routed to "some other" tenant. The real-DB
end-to-end counterpart lives in tests/replay/test_tenant_isolation.py.
"""

import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.models.client import Client
from app.services.tenant_routing import find_active_client_by_channel_id
from tests.test_webhook import VALID_PAYLOAD


def _db_returning(clients: list) -> MagicMock:
    """Session whose execute() yields a result whose scalars().all() is *clients*."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = clients
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _sig(body: bytes, secret: str = "test-app-secret") -> str:
    """Valid X-Hub-Signature-256 header for *body*."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- find_active_client_by_channel_id ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, ""])
async def test_missing_channel_id_returns_none_without_querying(missing):
    db = _db_returning([MagicMock(id=1)])

    result = await find_active_client_by_channel_id(
        db, Client.whatsapp_phone_number_id, missing, label="phone_number_id"
    )

    assert result is None
    db.execute.assert_not_called()  # in particular: no "first active client" lookup


@pytest.mark.asyncio
async def test_unmapped_channel_id_returns_none_and_logs(caplog):
    db = _db_returning([])

    with caplog.at_level(logging.WARNING):
        result = await find_active_client_by_channel_id(
            db, Client.whatsapp_phone_number_id, "999", label="phone_number_id"
        )

    assert result is None
    assert "phone_number_id=999" in caplog.text
    assert "dropped" in caplog.text
    assert db.execute.await_count == 1  # exactly one lookup, no second "fallback" query


@pytest.mark.asyncio
async def test_single_match_is_returned():
    tenant = MagicMock(id=7)
    db = _db_returning([tenant])

    result = await find_active_client_by_channel_id(
        db, Client.instagram_account_id, "ig-1", label="instagram_account_id"
    )

    assert result is tenant


@pytest.mark.asyncio
async def test_id_mapped_to_two_clients_is_dropped_not_guessed(caplog):
    db = _db_returning([MagicMock(id=1), MagicMock(id=2)])

    with caplog.at_level(logging.ERROR):
        result = await find_active_client_by_channel_id(
            db, Client.whatsapp_phone_number_id, "123", label="phone_number_id"
        )

    assert result is None
    assert "multiple active clients (1, 2)" in caplog.text


@pytest.mark.asyncio
async def test_query_filters_on_channel_id_and_active_only():
    db = _db_returning([])

    await find_active_client_by_channel_id(
        db, Client.whatsapp_phone_number_id, "123", label="phone_number_id"
    )

    sql = str(db.execute.call_args[0][0].compile(dialect=postgresql.dialect()))
    assert "clients.whatsapp_phone_number_id = " in sql
    assert "clients.is_active IS true" in sql or "clients.is_active = true" in sql
    assert "LIMIT" in sql


# --- WhatsApp router ---------------------------------------------------------

@patch("app.routers.webhook._send_typing_indicator", new_callable=AsyncMock)
@patch("app.routers.webhook.conversation_service.get_or_create_conversation", new_callable=AsyncMock)
@patch("app.routers.webhook._get_client_by_phone_number_id", new_callable=AsyncMock)
def test_whatsapp_unmapped_number_is_dropped_before_any_side_effect(
    mock_get_client, mock_conv, mock_typing, mock_db, client
):
    mock_get_client.return_value = None
    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    body = json.dumps(VALID_PAYLOAD).encode()

    response = client.post(
        "/webhook", content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sig(body)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "unmapped_number"}
    mock_conv.assert_not_called()
    mock_typing.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_get_client_by_phone_number_id_delegates_without_fallback():
    from app.routers.webhook import _get_client_by_phone_number_id

    db = _db_returning([])
    assert await _get_client_by_phone_number_id(db, "nope") is None
    assert await _get_client_by_phone_number_id(db, None) is None
    assert db.execute.await_count == 1  # None short-circuits; "nope" runs one query


# --- Instagram router --------------------------------------------------------

_IG_DM_PAYLOAD = {
    "object": "instagram",
    "entry": [{
        "id": "ig-unknown", "time": 1,
        "messaging": [{
            "sender": {"id": "cust1"}, "recipient": {"id": "ig-unknown"}, "timestamp": 1,
            "message": {"mid": "m1", "text": "hi"},
        }],
    }],
}


@patch("app.routers.instagram._handle_comment", new_callable=AsyncMock)
@patch("app.routers.instagram._handle_dm", new_callable=AsyncMock)
@patch("app.routers.instagram._get_active_client", new_callable=AsyncMock)
def test_instagram_unmapped_account_is_dropped(mock_get, mock_dm, mock_comment, client):
    mock_get.return_value = None
    body = json.dumps(_IG_DM_PAYLOAD).encode()

    response = client.post(
        "/instagram", content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sig(body)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "unmapped_account"}
    mock_dm.assert_not_called()
    mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_get_active_client_delegates_without_fallback():
    from app.routers.instagram import _get_active_client

    db = _db_returning([])
    assert await _get_active_client(db, "ig-x") is None
    assert await _get_active_client(db, None) is None
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_handle_dm_without_tenant_creates_no_conversation():
    from app.routers.instagram import _handle_dm
    from app.schemas.instagram import InstagramWebhookPayload

    dm = InstagramWebhookPayload.model_validate(_IG_DM_PAYLOAD).get_first_dm()

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=None)), \
         patch("app.routers.instagram.conversation_service.get_or_create_conversation",
               new=AsyncMock()) as mock_conv:
        result = await _handle_dm(db=MagicMock(), ig_user_id="ig-unknown", dm=dm)

    assert result == {"status": "unmapped_account"}
    mock_conv.assert_not_called()
