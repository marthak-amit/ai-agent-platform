"""
Centralized outbound send gate — the single policy choke point for every
message that leaves the platform toward a customer via Meta's WhatsApp /
Instagram APIs.

Why this exists: outbound messages used to leave from many code paths
(pipeline adapter, 6h nudge, follow-up engine, broadcasts, payment
confirmations, dashboard manual sends), each with its own — or no — check
for Meta's 24-hour customer-service window, opt-out, and block status.
Scattered per-caller checks can never be a 100% fix; any new sender
reintroduces the bug. check_send() is the one function that decides
whether a send may happen, and app/services/outbound.py is the only module
allowed to perform the actual API call (enforced by
tests/test_send_gate_guard.py).

Meta rules encoded here:
  * Free-form (non-template) business-initiated messages are allowed only
    within 24h of the customer's last inbound message. Outside that window
    only pre-approved template messages may be sent.
  * A reply to a fresh inbound message is always inside the window by
    definition (the inbound opened/refreshed it).
  * Instagram Private Replies (DM to a commenter) are allowed once per
    comment, within 7 days of the comment.
  * Marketing sends additionally require that the customer has not opted
    out — consent is stricter than the service window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

logger = logging.getLogger(__name__)

#: Meta's customer-service window for free-form messages.
WINDOW_HOURS = 24

#: Meta's window for a Private Reply to an Instagram comment.
COMMENT_WINDOW_DAYS = 7


class MessageKind(str, Enum):
    """What triggered this outbound message. Every send must declare one."""

    PIPELINE_REPLY = "pipeline_reply"          # direct reply to an inbound message
    NUDGE = "nudge"                            # 6h abandoned-intent auto-nudge
    FOLLOWUP = "followup"                      # lead re-engagement engine
    BROADCAST_MARKETING = "broadcast_marketing"  # campaign/broadcast engine
    UTILITY_TEMPLATE = "utility_template"      # transactional (payment confirm, invoice, status)
    MANUAL_AGENT = "manual_agent"              # human seller sending from the dashboard
    OWNER_ALERT = "owner_alert"                # platform → seller notification (recipient is not a customer)


class Verdict(str, Enum):
    """Outcome class of a gate check."""

    ALLOW = "allow"                            # free-form send permitted
    ALLOW_TEMPLATE_ONLY = "allow_template_only"  # only a pre-approved Meta template may be sent
    DENY = "deny"                              # nothing may be sent


class DenyReason(str, Enum):
    """Why a send was refused (or downgraded)."""

    OPTED_OUT = "opted_out"
    BLOCKED = "blocked"
    WINDOW_CLOSED = "window_closed"
    NO_TEMPLATE_AVAILABLE = "no_template_available"


#: Kinds that are business-initiated (no fresh inbound behind them) and must
#: therefore pass the 24h-window check.
PROACTIVE_KINDS = frozenset({
    MessageKind.NUDGE,
    MessageKind.FOLLOWUP,
    MessageKind.BROADCAST_MARKETING,
    MessageKind.UTILITY_TEMPLATE,
    MessageKind.MANUAL_AGENT,
})


@dataclass(frozen=True)
class SendDecision:
    """Result of check_send(). Immutable so callers can't 'fix' a DENY."""

    verdict: Verdict
    reason: DenyReason | None = None

    @property
    def allowed(self) -> bool:
        """True only for an unrestricted free-form ALLOW."""
        return self.verdict is Verdict.ALLOW

    @property
    def denied(self) -> bool:
        """True when nothing at all may be sent."""
        return self.verdict is Verdict.DENY


def log_suppression(
    *,
    customer_id: int | None,
    client_id: int | None,
    message_kind: MessageKind,
    channel: str,
    reason: DenyReason,
    detail: str = "",
) -> None:
    """
    Structured log line for every suppressed send.

    One greppable event name so a future dashboard "suppressed sends" view
    can be built from log ingestion without a schema change.
    """
    logger.warning(
        "event=send_suppressed customer_id=%s client_id=%s message_kind=%s "
        "channel=%s reason=%s detail=%s ts=%s",
        customer_id, client_id, message_kind.value, channel, reason.value,
        detail, datetime.now(timezone.utc).isoformat(),
    )


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive DB timestamp to UTC-aware for safe comparison.

    Non-datetime values (bad data, test doubles) are treated as absent —
    for window purposes that fails CLOSED, never open.
    """
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _is_window_open(db, customer, conversation_id: int | None) -> bool | None:
    """
    Resolve the 24h customer-service window.

    Prefers the conversation's own message history (windows are per channel,
    and conversations are per channel), falling back to the customer-level
    last_inbound_at snapshot (e.g. broadcast recipients resolved without a
    conversation).

    Returns:
        True   — window verifiably open (free-form OK).
        False  — window verifiably closed.
        None   — no inbound evidence at all (must be treated as closed for
                 free-form purposes; Meta requires a template to initiate).
    """
    if db is not None and conversation_id is not None:
        from app.services import messaging_window

        last_inbound = _as_utc(
            await messaging_window.get_last_inbound_at(db, conversation_id)
        )
        if last_inbound is not None:
            return (datetime.now(timezone.utc) - last_inbound) <= timedelta(hours=WINDOW_HOURS)
        # No inbound on this conversation — fall through to customer-level data.

    last_inbound = _as_utc(getattr(customer, "last_inbound_at", None)) if customer is not None else None
    if last_inbound is None:
        return None
    return (datetime.now(timezone.utc) - last_inbound) <= timedelta(hours=WINDOW_HOURS)


async def check_send(
    db=None,
    *,
    client_id: int | None = None,
    customer=None,
    message_kind: MessageKind,
    channel: str = "whatsapp",
    conversation_id: int | None = None,
    is_optout_confirmation: bool = False,
    comment_created_at: datetime | None = None,
) -> SendDecision:
    """
    Decide whether an outbound message may be sent. Rules, in order:

      1. OWNER_ALERT → ALLOW. The recipient is the seller, not a customer;
         opt-out/window semantics don't apply. (Still routed through
         outbound.py so the CI guard covers it.)
      2. customer.opted_out → DENY(OPTED_OUT) for every kind. Sole exception:
         the single opt-out confirmation message, requested explicitly via
         is_optout_confirmation=True and allowed only while
         customer.optout_confirmed_at is still NULL (one-time, DB-enforced —
         the sender stamps it after a successful send).
      3. customer.is_blocked → DENY(BLOCKED) for every kind.
      4. PIPELINE_REPLY → ALLOW: an inbound message by definition opens or
         refreshes the 24h window. For channel="instagram_comment" (Private
         Reply) the window is instead 7 days from the comment timestamp.
      5. Proactive kinds (NUDGE / FOLLOWUP / BROADCAST_MARKETING /
         UTILITY_TEMPLATE / MANUAL_AGENT): inside the 24h window → ALLOW
         free-form; outside (or no inbound evidence) → ALLOW_TEMPLATE_ONLY.
         Callers without an approved template must not send at all — the
         outbound wrappers convert that into DENY(WINDOW_CLOSED /
         NO_TEMPLATE_AVAILABLE) and log it.

    The window is re-checked at send time by design: a job scheduled at
    23h59m that actually runs at 24h01m gets refused here regardless of what
    the scheduler assumed.

    Args:
        db:                     Async session for window lookups (optional; without
                                it only customer-level data is used).
        client_id:              Owning client, for suppression logging.
        customer:               Customer ORM row (or None when no profile exists).
        message_kind:           What triggered this send.
        channel:                "whatsapp" | "instagram" | "instagram_comment".
        conversation_id:        Conversation whose inbound history defines the window.
        is_optout_confirmation: Explicit flag for the single goodbye message.
        comment_created_at:     For instagram_comment: when the comment was made.

    Returns:
        SendDecision — every DENY is logged before returning.
    """
    def _deny(reason: DenyReason, detail: str = "") -> SendDecision:
        log_suppression(
            customer_id=getattr(customer, "id", None),
            client_id=client_id if client_id is not None else getattr(customer, "client_id", None),
            message_kind=message_kind,
            channel=channel,
            reason=reason,
            detail=detail,
        )
        return SendDecision(Verdict.DENY, reason)

    # ── Rule 1: owner alerts — recipient is the seller, not a customer ──────
    if message_kind is MessageKind.OWNER_ALERT:
        return SendDecision(Verdict.ALLOW)

    # ── Rule 2: blocked customers get nothing — not even the opt-out
    # confirmation, which is why this outranks the opted_out exception. ────
    if customer is not None and getattr(customer, "is_blocked", False):
        return _deny(DenyReason.BLOCKED)

    # ── Rule 3: opt-out beats every remaining kind (consent & DND) ─────────
    if customer is not None and getattr(customer, "opted_out", False):
        if is_optout_confirmation and getattr(customer, "optout_confirmed_at", None) is None:
            # The one goodbye message confirming the opt-out. One-time:
            # the sender stamps optout_confirmed_at after a successful send.
            return SendDecision(Verdict.ALLOW)
        return _deny(DenyReason.OPTED_OUT)

    # ── Rule 4: replies to a fresh inbound are inside the window ───────────
    if message_kind is MessageKind.PIPELINE_REPLY:
        if channel == "instagram_comment":
            # Private Reply: 7 days from the comment, once per comment
            # (per-comment uniqueness is tracked in ig_comment_replies).
            created = _as_utc(comment_created_at)
            if created is not None and (
                datetime.now(timezone.utc) - created > timedelta(days=COMMENT_WINDOW_DAYS)
            ):
                return _deny(DenyReason.WINDOW_CLOSED, detail="comment_older_than_7d")
            return SendDecision(Verdict.ALLOW)
        return SendDecision(Verdict.ALLOW)

    # ── Rule 5: proactive sends must pass the 24h window ───────────────────
    window_open = await _is_window_open(db, customer, conversation_id)
    if window_open is True:
        return SendDecision(Verdict.ALLOW)

    # Window closed or unknown: only an approved template may go out.
    # (Not logged here — this verdict alone suppresses nothing. The outbound
    # wrapper logs the DENY if the caller has no template to fall back to.)
    return SendDecision(Verdict.ALLOW_TEMPLATE_ONLY, DenyReason.WINDOW_CLOSED)
