"""
Tests for app/services/lead_service.py.

classify_lead (app.services.intent_service) is synchronous, stage-first with
keyword fallback — no AI call. lead_service._classify was replaced by it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import lead_service
from app.services.intent_service import classify_lead


@pytest.fixture
def db():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


async def test_tag_lead_creates_new_lead(db, mock_settings):
    """Creates a new Lead when none exists for this phone number."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result

    await lead_service.tag_lead(db, "919999999999", 1, [{"role": "user", "content": "order karna hai"}])

    db.add.assert_called_once()
    db.commit.assert_called_once()


async def test_tag_lead_updates_existing_lead(db, mock_settings):
    """Updates status on an existing Lead."""
    from app.models.lead import Lead

    existing = Lead(phone_number="919999999999", status="cold")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    db.execute.return_value = mock_result

    await lead_service.tag_lead(db, "919999999999", 1, [{"role": "user", "content": "order karna hai"}])

    assert existing.status == "hot"
    db.commit.assert_called_once()


def test_classify_returns_hot_on_order_keyword(mock_settings):
    """classify_lead returns 'hot' for order-intent keywords."""
    result = classify_lead("order karna hai", "greeting")
    assert result == "hot"


def test_classify_returns_warm_on_price_keyword(mock_settings):
    """classify_lead returns 'warm' for browsing-signal keywords."""
    result = classify_lead("delivery time kitna lagega", "greeting")
    assert result == "warm"


def test_classify_returns_cold_on_empty_messages(mock_settings):
    """classify_lead returns 'cold' for an empty message."""
    result = classify_lead("", "greeting")
    assert result == "cold"


def test_classify_returns_cold_on_no_keywords(mock_settings):
    """classify_lead returns 'cold' when no hot/warm keywords found."""
    result = classify_lead("hello", "greeting")
    assert result == "cold"
