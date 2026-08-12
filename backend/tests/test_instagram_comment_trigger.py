"""
Tests for the IG comment → private-reply DM auto-trigger.

Covers:
  - app.services.ig_comment_service: matches_trigger, is_comment_dm_rate_limited,
    send_comment_reply (public reply + pipeline call + private reply, with the
    plain-text button/list fallback), get_comment_reply_stats
  - app.routers.instagram._handle_comment: gating (disabled feature, no keyword
    match, dedup, reply-all override), rate-limit queuing
  - app.scheduler._drain_pending_comment_replies: drains queued rows respecting
    the same rate limiter, skips inactive clients

All DB and external-service calls are mocked so no real DB or API is needed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.client import Client
from app.models.ig_comment_reply import IgCommentReply
from app.schemas.instagram import CommentChange, CommentValue
from app.services import ig_comment_service


def _comment(text: str, comment_id: str = "cmt1", commenter_igsid: str = "igsid1", media_id: str | None = "media1") -> CommentChange:
    return CommentChange(
        field="comments",
        value=CommentValue(
            **{"from": {"id": commenter_igsid, "username": "sim_user"}},
            media={"id": media_id} if media_id else None,
            id=comment_id,
            text=text,
        ),
    )


def _client(
    client_id: int = 5,
    autoreply_enabled: bool = True,
    reply_all: bool = False,
    triggers=None,
    reply_text=None,
    is_active: bool = True,
    instagram_account_id: str = "ig_biz_1",
) -> Client:
    c = Client()
    c.id = client_id
    c.is_active = is_active
    c.instagram_account_id = instagram_account_id
    c.ig_comment_autoreply_enabled = autoreply_enabled
    c.ig_comment_reply_all = reply_all
    c.ig_comment_triggers = triggers if triggers is not None else ["price", "order"]
    c.ig_comment_reply_text = reply_text if reply_text is not None else {
        "english": "Check your DM 👀", "hindi": "DM dekho", "gujarati": "DM joi lo",
    }
    return c


def _log_row(**kwargs) -> IgCommentReply:
    row = IgCommentReply()
    row.client_id = kwargs.get("client_id", 5)
    row.comment_id = kwargs.get("comment_id", "cmt1")
    row.commenter_igsid = kwargs.get("commenter_igsid", "igsid1")
    row.media_id = kwargs.get("media_id", "media1")
    row.comment_text = kwargs.get("comment_text", "what is the price?")
    row.status = kwargs.get("status", "pending")
    return row


def _scalar_result(obj):
    m = MagicMock()
    m.scalar_one_or_none.return_value = obj
    return m


def _candidates_result(rows):
    m = MagicMock()
    m.scalars.return_value.all.return_value = rows
    return m


# ── matches_trigger ───────────────────────────────────────────────────────────

def test_matches_trigger_case_insensitive():
    assert ig_comment_service.matches_trigger("What is the PRICE?", ["price", "order"]) is True


def test_matches_trigger_no_match():
    assert ig_comment_service.matches_trigger("nice colour!", ["price", "order"]) is False


def test_matches_trigger_empty_triggers():
    assert ig_comment_service.matches_trigger("price check", None) is False


# ── is_comment_dm_rate_limited ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_allows_under_cap():
    ig_comment_service._comment_rate_limit_store.clear()
    assert await ig_comment_service.is_comment_dm_rate_limited("acct_under_cap") is False
    ig_comment_service._comment_rate_limit_store.clear()


@pytest.mark.asyncio
async def test_rate_limit_blocks_at_cap():
    ig_comment_service._comment_rate_limit_store.clear()
    acct = "acct_at_cap"
    for _ in range(ig_comment_service._COMMENT_RATE_LIMIT_MESSAGES):
        assert await ig_comment_service.is_comment_dm_rate_limited(acct) is False
    assert await ig_comment_service.is_comment_dm_rate_limited(acct) is True
    ig_comment_service._comment_rate_limit_store.clear()


# ── _handle_comment gating ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_comment_skips_when_no_active_client(mock_db):
    from app.routers.instagram import _handle_comment

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=None)):
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("what is the price?"))

    assert result == {"status": "ok"}
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_handle_comment_skips_duplicate_comment_id(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(IgCommentReply())

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client())), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("price?", comment_id="dup1"))

    assert result == {"status": "ok"}
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_handle_comment_skips_when_autoreply_disabled(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(None)

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client(autoreply_enabled=False))), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("what is the price?"))

    assert result == {"status": "ok"}
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_handle_comment_skips_when_no_keyword_match(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(None)

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client(reply_all=False, triggers=["price", "order"]))), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("nice colour!"))

    assert result == {"status": "ok"}
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_handle_comment_reply_all_ignores_keyword_gate(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(None)

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client(reply_all=True, triggers=["price"]))), \
         patch("app.services.ig_comment_service.is_comment_dm_rate_limited", new=AsyncMock(return_value=False)), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("nice colour!"))

    assert result == {"status": "ok"}
    send_mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_comment_matched_keyword_triggers_send(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(None)

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client())), \
         patch("app.services.ig_comment_service.is_comment_dm_rate_limited", new=AsyncMock(return_value=False)), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("what is the price?"))

    assert result == {"status": "ok"}
    send_mock.assert_called_once()
    assert mock_db.add.call_count == 1
    added_row = mock_db.add.call_args[0][0]
    assert added_row.comment_text == "what is the price?"
    assert added_row.status == "pending"


@pytest.mark.asyncio
async def test_handle_comment_rate_limited_queues_without_sending(mock_db):
    from app.routers.instagram import _handle_comment

    mock_db.execute.return_value = _scalar_result(None)

    with patch("app.routers.instagram._get_active_client", new=AsyncMock(return_value=_client())), \
         patch("app.services.ig_comment_service.is_comment_dm_rate_limited", new=AsyncMock(return_value=True)), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        result = await _handle_comment(mock_db, "ig_biz_1", _comment("what is the price?"))

    assert result == {"status": "ok"}
    send_mock.assert_not_called()
    assert mock_db.add.call_count == 1


# ── send_comment_reply ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_comment_reply_success_sends_public_and_private_reply(mock_db):
    client = _client()
    row = _log_row(comment_text="what is the price?")
    conv = SimpleNamespace(id=99)
    pipeline_result = SimpleNamespace(text="It's ₹999!", buttons=None, list_options=None)

    with patch("app.services.instagram_service._raw_reply_to_comment", new=AsyncMock()) as reply_mock, \
         patch("app.services.ig_comment_service.conversation_service.get_or_create_conversation", new=AsyncMock(return_value=conv)) as conv_mock, \
         patch("app.services.ig_comment_service.handle_inbound_message", new=AsyncMock(return_value=pipeline_result)), \
         patch("app.services.instagram_service._raw_send_private_reply", new=AsyncMock()) as private_mock:
        await ig_comment_service.send_comment_reply(mock_db, client, "ig_biz_1", row)

    reply_mock.assert_called_once_with(
        ig_user_id="ig_biz_1", comment_id="cmt1", message_text="Check your DM 👀"
    )
    assert conv_mock.call_args.kwargs["source"] == "comment_reply"
    private_mock.assert_called_once_with(
        ig_user_id="ig_biz_1", comment_id="cmt1", message_text="It's ₹999!"
    )
    assert row.status == "sent"
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_send_comment_reply_uses_hindi_bucket_for_hindi_comment(mock_db):
    client = _client(reply_text={"english": "Check DM", "hindi": "DM dekho", "gujarati": "DM joi lo"})
    row = _log_row(comment_text="kitna hai price")
    conv = SimpleNamespace(id=99)
    pipeline_result = SimpleNamespace(text="reply", buttons=None, list_options=None)

    with patch("app.services.instagram_service._raw_reply_to_comment", new=AsyncMock()) as reply_mock, \
         patch("app.services.ig_comment_service.conversation_service.get_or_create_conversation", new=AsyncMock(return_value=conv)), \
         patch("app.services.ig_comment_service.handle_inbound_message", new=AsyncMock(return_value=pipeline_result)), \
         patch("app.services.instagram_service._raw_send_private_reply", new=AsyncMock()):
        await ig_comment_service.send_comment_reply(mock_db, client, "ig_biz_1", row)

    reply_mock.assert_called_once_with(
        ig_user_id="ig_biz_1", comment_id="cmt1", message_text="DM dekho"
    )


@pytest.mark.asyncio
async def test_send_comment_reply_buttons_collapse_to_numbered_text(mock_db):
    from app.services.order_pipeline import ButtonSpec

    client = _client()
    row = _log_row()
    conv = SimpleNamespace(id=99)
    pipeline_result = SimpleNamespace(
        text="Pick a size:",
        buttons=[ButtonSpec(id="S", title="Small"), ButtonSpec(id="M", title="Medium")],
        list_options=None,
    )

    with patch("app.services.instagram_service._raw_reply_to_comment", new=AsyncMock()), \
         patch("app.services.ig_comment_service.conversation_service.get_or_create_conversation", new=AsyncMock(return_value=conv)), \
         patch("app.services.ig_comment_service.handle_inbound_message", new=AsyncMock(return_value=pipeline_result)), \
         patch("app.services.instagram_service._raw_send_private_reply", new=AsyncMock()) as private_mock:
        await ig_comment_service.send_comment_reply(mock_db, client, "ig_biz_1", row)

    sent_text = private_mock.call_args.kwargs["message_text"]
    assert "Pick a size:" in sent_text
    assert "1. Small" in sent_text
    assert "2. Medium" in sent_text


@pytest.mark.asyncio
async def test_send_comment_reply_private_send_failure_marks_row_failed(mock_db):
    client = _client()
    row = _log_row()
    conv = SimpleNamespace(id=99)
    pipeline_result = SimpleNamespace(text="reply", buttons=None, list_options=None)

    with patch("app.services.instagram_service._raw_reply_to_comment", new=AsyncMock()), \
         patch("app.services.ig_comment_service.conversation_service.get_or_create_conversation", new=AsyncMock(return_value=conv)), \
         patch("app.services.ig_comment_service.handle_inbound_message", new=AsyncMock(return_value=pipeline_result)), \
         patch("app.services.instagram_service._raw_send_private_reply", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await ig_comment_service.send_comment_reply(mock_db, client, "ig_biz_1", row)

    assert row.status == "failed"


@pytest.mark.asyncio
async def test_send_comment_reply_public_reply_failure_still_attempts_private_reply(mock_db):
    client = _client()
    row = _log_row()
    conv = SimpleNamespace(id=99)
    pipeline_result = SimpleNamespace(text="reply", buttons=None, list_options=None)

    with patch("app.services.instagram_service._raw_reply_to_comment", new=AsyncMock(side_effect=RuntimeError("public fail"))), \
         patch("app.services.ig_comment_service.conversation_service.get_or_create_conversation", new=AsyncMock(return_value=conv)), \
         patch("app.services.ig_comment_service.handle_inbound_message", new=AsyncMock(return_value=pipeline_result)), \
         patch("app.services.instagram_service._raw_send_private_reply", new=AsyncMock()) as private_mock:
        await ig_comment_service.send_comment_reply(mock_db, client, "ig_biz_1", row)

    private_mock.assert_called_once()
    assert row.status == "sent"


# ── get_comment_reply_stats ───────────────────────────────────────────────────

def _scalar_one(n):
    m = MagicMock()
    m.scalar_one.return_value = n
    return m


@pytest.mark.asyncio
async def test_get_comment_reply_stats_returns_expected_shape(mock_db):
    mock_db.execute.side_effect = [_scalar_one(4), _scalar_one(10), _scalar_one(3)]

    stats = await ig_comment_service.get_comment_reply_stats(mock_db, client_id=5)

    assert stats == {
        "today_sent": 4,
        "total_comment_conversations": 10,
        "converted_conversations": 3,
        "conversion_rate": 0.3,
    }


@pytest.mark.asyncio
async def test_get_comment_reply_stats_zero_conversations_no_division_error(mock_db):
    mock_db.execute.side_effect = [_scalar_one(0), _scalar_one(0), _scalar_one(0)]

    stats = await ig_comment_service.get_comment_reply_stats(mock_db, client_id=5)
    assert stats["conversion_rate"] == 0.0


# ── scheduler drain job ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_sends_pending_rows_under_cap(mock_db):
    from app.scheduler import _drain_pending_comment_replies

    row = _log_row()
    client = _client()
    mock_db.execute.side_effect = [_candidates_result([row]), _scalar_result(client)]

    with patch("app.services.ig_comment_service.is_comment_dm_rate_limited", new=AsyncMock(return_value=False)), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        await _drain_pending_comment_replies(mock_db)

    send_mock.assert_called_once_with(mock_db, client, client.instagram_account_id, row)


@pytest.mark.asyncio
async def test_drain_leaves_row_pending_when_still_over_cap(mock_db):
    from app.scheduler import _drain_pending_comment_replies

    row = _log_row()
    client = _client()
    mock_db.execute.side_effect = [_candidates_result([row]), _scalar_result(client)]

    with patch("app.services.ig_comment_service.is_comment_dm_rate_limited", new=AsyncMock(return_value=True)), \
         patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        await _drain_pending_comment_replies(mock_db)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_drain_skips_inactive_client(mock_db):
    from app.scheduler import _drain_pending_comment_replies

    row = _log_row()
    client = _client(is_active=False)
    mock_db.execute.side_effect = [_candidates_result([row]), _scalar_result(client)]

    with patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        await _drain_pending_comment_replies(mock_db)

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_drain_no_pending_rows_is_noop(mock_db):
    from app.scheduler import _drain_pending_comment_replies

    mock_db.execute.return_value = _candidates_result([])

    with patch("app.services.ig_comment_service.send_comment_reply", new=AsyncMock()) as send_mock:
        await _drain_pending_comment_replies(mock_db)

    send_mock.assert_not_called()
