"""
Tests for app/services/conversation_service.py.

All DB interactions use an AsyncMock session — no real DB connection.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import conversation_service


@pytest.fixture
def db():
    """Mock AsyncSession with execute/commit/refresh/add."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


async def test_get_or_create_returns_existing_conversation(db, mock_settings):
    """Returns an existing Conversation without inserting a new one."""
    from app.models.conversation import Conversation

    existing = Conversation(id=1, phone_number="919999999999", channel="whatsapp")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    db.execute.return_value = mock_result

    conv = await conversation_service.get_or_create_conversation(db, "919999999999")

    assert conv.id == 1
    db.add.assert_not_called()
    db.commit.assert_not_called()


async def test_get_or_create_creates_new_conversation(db, mock_settings):
    """Creates and commits a new Conversation when none exists."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result

    await conversation_service.get_or_create_conversation(db, "919999999999")

    db.add.assert_called_once()
    db.commit.assert_called_once()
    db.refresh.assert_called_once()


async def test_save_message_adds_and_commits(db, mock_settings):
    """save_message adds a Message and commits."""
    await conversation_service.save_message(db, conversation_id=1, role="user", content="Hi")

    db.add.assert_called_once()
    db.commit.assert_called_once()
    db.refresh.assert_called_once()


async def test_get_history_returns_messages(db, mock_settings):
    """get_history returns a list of messages from the query result."""
    from app.models.message import Message

    msgs = [
        Message(id=1, conversation_id=1, role="user", content="Hello"),
        Message(id=2, conversation_id=1, role="model", content="Hi there"),
    ]
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = msgs
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    db.execute.return_value = mock_result

    result = await conversation_service.get_history(db, conversation_id=1)

    assert len(result) == 2
    assert result[0].role == "user"
    assert result[1].role == "assistant"
