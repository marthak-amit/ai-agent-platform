"""
Tests for the plan-guard functions in app/services/campaign_service.py.

These moved from a hardcoded _PLAN_LIMITS dict to reading the `plans` DB
table (via plan_cache) — see the `seeded_plans` fixture in conftest.py.
"""

from unittest.mock import MagicMock

import pytest

from app.services import campaign_service


# ── check_plan_allows_campaigns ───────────────────────────────────────────────

async def test_starter_blocks_campaigns(mock_db, seeded_plans):
    """Starter plan does not allow campaigns."""
    with pytest.raises(ValueError, match="Growth or Pro"):
        await campaign_service.check_plan_allows_campaigns(mock_db, "starter")


async def test_growth_allows_campaigns(mock_db, seeded_plans):
    """Growth plan allows campaigns."""
    await campaign_service.check_plan_allows_campaigns(mock_db, "growth")  # no raise


# ── check_recipient_limit ─────────────────────────────────────────────────────

async def test_growth_recipient_limit_enforced(mock_db, seeded_plans):
    """Growth's 500-recipient cap raises when exceeded."""
    with pytest.raises(ValueError, match="maximum of 500"):
        await campaign_service.check_recipient_limit(mock_db, "growth", 600)


async def test_growth_recipient_limit_within_cap(mock_db, seeded_plans):
    """Growth allows up to 500 recipients."""
    await campaign_service.check_recipient_limit(mock_db, "growth", 500)  # no raise


async def test_pro_recipient_limit_unlimited(mock_db, seeded_plans):
    """Pro's 99999 sentinel means no recipient cap."""
    await campaign_service.check_recipient_limit(mock_db, "pro", 50000)  # no raise


# ── check_monthly_campaign_limit ──────────────────────────────────────────────

async def test_growth_monthly_campaign_limit_enforced(mock_db, seeded_plans):
    """Growth's 1-campaign/month cap raises once already used."""
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    mock_db.execute.return_value = count_result

    with pytest.raises(ValueError, match="1 campaign"):
        await campaign_service.check_monthly_campaign_limit(mock_db, "growth", client_id=1)


async def test_pro_monthly_campaign_limit_unlimited(mock_db, seeded_plans):
    """Pro's 99999 sentinel means no monthly campaign cap (no DB count needed)."""
    await campaign_service.check_monthly_campaign_limit(mock_db, "pro", client_id=1)  # no raise
    mock_db.execute.assert_not_called()
