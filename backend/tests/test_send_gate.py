"""
Unit-test matrix for the centralized outbound send gate.

Covers every message_kind × customer-state combination from the gate rule
table: opt-out (with the one-time confirmation exception), blocked, the 24h
window at 23h/25h boundaries, pipeline replies always allowed, the IG
private-reply 7-day comment window, and the outbound wrappers refusing to
free-form-send on ALLOW_TEMPLATE_ONLY (zero raw API calls).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.send_gate import (
    DenyReason,
    MessageKind,
    Verdict,
    check_send,
)

pytestmark = pytest.mark.asyncio


def _customer(
    *,
    opted_out: bool = False,
    is_blocked: bool = False,
    last_inbound_hours_ago: float | None = None,
    optout_confirmed_at: datetime | None = None,
) -> SimpleNamespace:
    """Build a lightweight customer double with gate-relevant fields."""
    last_inbound_at = (
        datetime.now(timezone.utc) - timedelta(hours=last_inbound_hours_ago)
        if last_inbound_hours_ago is not None
        else None
    )
    return SimpleNamespace(
        id=1,
        client_id=7,
        opted_out=opted_out,
        is_blocked=is_blocked,
        optout_confirmed_at=optout_confirmed_at,
        last_inbound_at=last_inbound_at,
    )


PROACTIVE = [
    MessageKind.NUDGE,
    MessageKind.FOLLOWUP,
    MessageKind.BROADCAST_MARKETING,
    MessageKind.UTILITY_TEMPLATE,
    MessageKind.MANUAL_AGENT,
]
ALL_CUSTOMER_KINDS = [MessageKind.PIPELINE_REPLY, *PROACTIVE]


# ── Rule 1-2: opt-out beats everything ───────────────────────────────────────

@pytest.mark.parametrize("kind", ALL_CUSTOMER_KINDS)
async def test_opted_out_denies_every_kind(kind):
    """An opted-out customer gets nothing — broadcast, follow-up, nudge, or reply."""
    customer = _customer(opted_out=True, last_inbound_hours_ago=1)
    decision = await check_send(None, customer=customer, message_kind=kind)
    assert decision.verdict is Verdict.DENY
    assert decision.reason is DenyReason.OPTED_OUT


async def test_optout_confirmation_allowed_exactly_once():
    """The goodbye message is allowed while unconfirmed, denied after stamping."""
    customer = _customer(opted_out=True)
    first = await check_send(
        None, customer=customer,
        message_kind=MessageKind.PIPELINE_REPLY,
        is_optout_confirmation=True,
    )
    assert first.verdict is Verdict.ALLOW

    customer.optout_confirmed_at = datetime.now(timezone.utc)
    second = await check_send(
        None, customer=customer,
        message_kind=MessageKind.PIPELINE_REPLY,
        is_optout_confirmation=True,
    )
    assert second.verdict is Verdict.DENY
    assert second.reason is DenyReason.OPTED_OUT


# ── Rule 3: blocked ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ALL_CUSTOMER_KINDS)
async def test_blocked_denies_every_kind(kind):
    """A blocked customer gets nothing, on every path."""
    customer = _customer(is_blocked=True, last_inbound_hours_ago=1)
    decision = await check_send(None, customer=customer, message_kind=kind)
    assert decision.verdict is Verdict.DENY
    assert decision.reason is DenyReason.BLOCKED


async def test_blocked_beats_optout_confirmation():
    """Even the opt-out confirmation never goes to a blocked customer."""
    customer = _customer(opted_out=True, is_blocked=True)
    decision = await check_send(
        None, customer=customer,
        message_kind=MessageKind.PIPELINE_REPLY,
        is_optout_confirmation=True,
    )
    # Opt-out confirmation passes rule 2, but rule 3 still denies.
    assert decision.verdict is Verdict.DENY
    assert decision.reason is DenyReason.BLOCKED


# ── Rule 4: pipeline replies ─────────────────────────────────────────────────

async def test_pipeline_reply_allowed_even_with_stale_window():
    """A reply to a fresh inbound is allowed regardless of stored window data."""
    customer = _customer(last_inbound_hours_ago=25)
    decision = await check_send(
        None, customer=customer, message_kind=MessageKind.PIPELINE_REPLY
    )
    assert decision.verdict is Verdict.ALLOW


async def test_pipeline_reply_allowed_with_no_customer_row():
    """First-contact inbound (no profile yet) must never be blocked."""
    decision = await check_send(
        None, customer=None, message_kind=MessageKind.PIPELINE_REPLY
    )
    assert decision.verdict is Verdict.ALLOW


async def test_ig_private_reply_within_7_days_allowed():
    """Private Reply to a 2-day-old comment is inside Meta's 7-day window."""
    decision = await check_send(
        None, customer=None,
        message_kind=MessageKind.PIPELINE_REPLY,
        channel="instagram_comment",
        comment_created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    assert decision.verdict is Verdict.ALLOW


async def test_ig_private_reply_after_7_days_denied():
    """Private Reply to an 8-day-old comment is refused (WINDOW_CLOSED)."""
    decision = await check_send(
        None, customer=None,
        message_kind=MessageKind.PIPELINE_REPLY,
        channel="instagram_comment",
        comment_created_at=datetime.now(timezone.utc) - timedelta(days=8),
    )
    assert decision.verdict is Verdict.DENY
    assert decision.reason is DenyReason.WINDOW_CLOSED


# ── Rule 5: the 24h window for proactive sends ───────────────────────────────

@pytest.mark.parametrize("kind", PROACTIVE)
async def test_proactive_inside_window_allows_freeform(kind):
    """last_inbound 23h ago → free-form proactive send is allowed."""
    customer = _customer(last_inbound_hours_ago=23)
    decision = await check_send(None, customer=customer, message_kind=kind)
    assert decision.verdict is Verdict.ALLOW


@pytest.mark.parametrize("kind", PROACTIVE)
async def test_proactive_outside_window_template_only(kind):
    """last_inbound 25h ago → template-only; free-form must not be sent."""
    customer = _customer(last_inbound_hours_ago=25)
    decision = await check_send(None, customer=customer, message_kind=kind)
    assert decision.verdict is Verdict.ALLOW_TEMPLATE_ONLY
    assert decision.reason is DenyReason.WINDOW_CLOSED


@pytest.mark.parametrize("kind", PROACTIVE)
async def test_proactive_with_no_inbound_evidence_template_only(kind):
    """No inbound ever (imported number) → Meta requires a template to initiate."""
    customer = _customer(last_inbound_hours_ago=None)
    decision = await check_send(None, customer=customer, message_kind=kind)
    assert decision.verdict is Verdict.ALLOW_TEMPLATE_ONLY


# ── Rule 1: owner alerts ─────────────────────────────────────────────────────

async def test_owner_alert_always_allowed():
    """Owner alerts target the seller — customer rules don't apply."""
    decision = await check_send(None, message_kind=MessageKind.OWNER_ALERT)
    assert decision.verdict is Verdict.ALLOW


# ── Outbound wrappers: suppression means ZERO raw API calls ──────────────────

async def test_outbound_send_text_suppressed_outside_window_makes_no_api_call():
    """FOLLOWUP at 25h: gate says template-only, no template exists → no send."""
    from app.services import outbound

    customer = _customer(last_inbound_hours_ago=25)
    with patch(
        "app.services.whatsapp_service._raw_send_text_message", new=AsyncMock()
    ) as raw:
        result = await outbound.send_text(
            "919900000001", "free-form follow-up",
            kind=MessageKind.FOLLOWUP, customer=customer,
        )
    assert result is None
    raw.assert_not_called()


async def test_outbound_send_text_suppressed_for_opted_out_customer():
    """Opt-out honored at the wrapper: raw transport is never touched."""
    from app.services import outbound

    customer = _customer(opted_out=True, last_inbound_hours_ago=1)
    with patch(
        "app.services.whatsapp_service._raw_send_text_message", new=AsyncMock()
    ) as raw:
        result = await outbound.send_text(
            "919900000001", "campaign blast",
            kind=MessageKind.BROADCAST_MARKETING, customer=customer,
        )
    assert result is None
    raw.assert_not_called()


async def test_outbound_send_text_allows_pipeline_reply():
    """Pipeline replies pass straight through to the raw transport."""
    from app.services import outbound

    customer = _customer(last_inbound_hours_ago=25)  # stale — irrelevant for replies
    with patch(
        "app.services.whatsapp_service._raw_send_text_message",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.x"}]}),
    ) as raw:
        result = await outbound.send_text(
            "919900000001", "here is your answer",
            kind=MessageKind.PIPELINE_REPLY, customer=customer,
        )
    assert result == {"messages": [{"id": "wamid.x"}]}
    raw.assert_called_once()


async def test_outbound_ig_dm_suppressed_for_blocked_customer():
    """The IG DM wrapper enforces the same gate as WhatsApp."""
    from app.services import outbound

    customer = _customer(is_blocked=True)
    with patch(
        "app.services.instagram_service._raw_send_dm", new=AsyncMock()
    ) as raw:
        result = await outbound.ig_send_dm(
            "ig_biz_1", "IGSID_9", "hello",
            kind=MessageKind.FOLLOWUP, customer=customer,
        )
    assert result is None
    raw.assert_not_called()
