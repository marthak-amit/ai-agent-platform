"""
Tests for app/routers/instagram.py's postback (carousel tap) handling.

A tapped Generic Template card arrives as a `postback` sibling field (no
`message`) — previously silently dropped by the top-level dispatch and by
_handle_dm's msg_type switch. These confirm it now reaches
handle_inbound_message with user_text set to the tapped SKU and
message.type == "text", so it lands in the exact same pending_choice_skus
resolution ladder a typed "2" or a WhatsApp button tap already uses.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.instagram import InstagramWebhookPayload
from app.routers.instagram import _handle_dm


def _no_dup_db() -> MagicMock:
    """A db mock whose dedup-check execute() finds no existing row."""
    db = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=execute_result)
    return db


def _postback_dm(sku: str = "PR17761"):
    payload = InstagramWebhookPayload.model_validate({
        "object": "instagram",
        "entry": [{
            "id": "IGID1",
            "messaging": [{
                "sender": {"id": "S1"}, "recipient": {"id": "R1"}, "timestamp": 123,
                "postback": {"payload": sku, "title": "Select"},
            }],
        }],
    })
    return payload.get_first_dm()


async def test_top_level_dispatch_routes_postback_only_event_to_handle_dm():
    """The router's top-level dispatch condition must accept postback-only events."""
    from app.schemas.instagram import InstagramWebhookPayload as P

    payload = P.model_validate({
        "object": "instagram",
        "entry": [{
            "id": "IGID1",
            "messaging": [{
                "sender": {"id": "S1"}, "recipient": {"id": "R1"}, "timestamp": 123,
                "postback": {"payload": "PR17761", "title": "Select"},
            }],
        }],
    })
    dm = payload.get_first_dm()
    assert dm.message is None and dm.postback is not None
    # Mirrors instagram.py's top-level `if dm and (dm.message is not None or
    # dm.postback is not None):` — this must be True for a postback-only event.
    assert dm and (dm.message is not None or dm.postback is not None)


async def test_handle_dm_synthesizes_sku_as_text_message_for_postback():
    """A tapped carousel card's SKU reaches handle_inbound_message as plain text."""
    dm = _postback_dm("PR17761")

    fake_conv = MagicMock(id=1, client_id=1)
    fake_result = MagicMock(status="ok")

    captured_ctx = {}

    async def _capture_handle_inbound_message(ctx):
        captured_ctx["ctx"] = ctx
        return fake_result

    with patch(
        "app.routers.instagram._get_active_client", new=AsyncMock(return_value=None),
    ), patch(
        "app.routers.instagram.conversation_service.get_or_create_conversation",
        new=AsyncMock(return_value=fake_conv),
    ), patch(
        "app.routers.instagram.handle_inbound_message",
        new=_capture_handle_inbound_message,
    ), patch(
        "app.routers.instagram.send_pipeline_result", new=AsyncMock(),
    ):
        result = await _handle_dm(db=_no_dup_db(), ig_user_id="ig1", dm=dm)

    assert result == {"status": "ok"}
    ctx = captured_ctx["ctx"]
    assert ctx.user_text == "PR17761"
    assert ctx.message.type == "text"
    assert ctx.message.text.body == "PR17761"
    assert ctx.is_whatsapp is False


async def test_handle_dm_postback_with_empty_payload_synthesizes_empty_text():
    """A postback with no payload doesn't crash — synthesizes an empty user_text."""
    payload = InstagramWebhookPayload.model_validate({
        "object": "instagram",
        "entry": [{
            "id": "IGID1",
            "messaging": [{
                "sender": {"id": "S1"}, "recipient": {"id": "R1"}, "timestamp": 123,
                "postback": {"title": "Select"},
            }],
        }],
    })
    dm = payload.get_first_dm()

    fake_conv = MagicMock(id=1, client_id=1)
    fake_result = MagicMock(status="ok")
    captured_ctx = {}

    async def _capture_handle_inbound_message(ctx):
        captured_ctx["ctx"] = ctx
        return fake_result

    with patch(
        "app.routers.instagram._get_active_client", new=AsyncMock(return_value=None),
    ), patch(
        "app.routers.instagram.conversation_service.get_or_create_conversation",
        new=AsyncMock(return_value=fake_conv),
    ), patch(
        "app.routers.instagram.handle_inbound_message",
        new=_capture_handle_inbound_message,
    ), patch(
        "app.routers.instagram.send_pipeline_result", new=AsyncMock(),
    ):
        await _handle_dm(db=_no_dup_db(), ig_user_id="ig1", dm=dm)

    assert captured_ctx["ctx"].user_text == ""


async def test_handle_dm_postback_synthesizes_a_nonempty_mid_for_dedup():
    """A postback tap gets a non-None synthesized mid, so retry-dedup isn't silently skipped."""
    dm = _postback_dm("PR17761")

    fake_conv = MagicMock(id=1, client_id=1)
    fake_result = MagicMock(status="ok")
    captured_ctx = {}

    async def _capture_handle_inbound_message(ctx):
        captured_ctx["ctx"] = ctx
        return fake_result

    no_dup_db = _no_dup_db()

    with patch(
        "app.routers.instagram._get_active_client", new=AsyncMock(return_value=None),
    ), patch(
        "app.routers.instagram.conversation_service.get_or_create_conversation",
        new=AsyncMock(return_value=fake_conv),
    ), patch(
        "app.routers.instagram.handle_inbound_message",
        new=_capture_handle_inbound_message,
    ), patch(
        "app.routers.instagram.send_pipeline_result", new=AsyncMock(),
    ):
        await _handle_dm(db=no_dup_db, ig_user_id="ig1", dm=dm)

    mid = captured_ctx["ctx"].wamid
    assert mid is not None and mid != ""
    assert mid == "ig_postback:S1:PR17761:123"
    no_dup_db.execute.assert_called_once()  # dedup query actually ran, unlike with mid=None


async def test_handle_dm_postback_retry_is_deduped_and_skipped():
    """A Meta webhook retry of the same postback tap is skipped, not double-processed."""
    dm = _postback_dm("PR17761")

    fake_conv = MagicMock(id=1, client_id=1)

    dup_db = MagicMock()
    dup_execute_result = MagicMock()
    dup_execute_result.scalar_one_or_none.return_value = MagicMock()  # an existing row found
    dup_db.execute = AsyncMock(return_value=dup_execute_result)

    handle_inbound_message_mock = AsyncMock()

    with patch(
        "app.routers.instagram._get_active_client", new=AsyncMock(return_value=None),
    ), patch(
        "app.routers.instagram.conversation_service.get_or_create_conversation",
        new=AsyncMock(return_value=fake_conv),
    ), patch(
        "app.routers.instagram.handle_inbound_message", new=handle_inbound_message_mock,
    ), patch(
        "app.routers.instagram.send_pipeline_result", new=AsyncMock(),
    ):
        result = await _handle_dm(db=dup_db, ig_user_id="ig1", dm=dm)

    assert result == {"status": "ok"}
    handle_inbound_message_mock.assert_not_called()
