"""
Runtime channel-health flags (in-process, reset on restart).

Tracks Instagram only. Two independent things:
- a process-wide kill switch (`disable_instagram`): IG sends are skipped with a
  clear (rate-limited) warning instead of failing on every attempt;
- per-client token verdicts ("valid"/"invalid"/"unknown") from the startup check and the
  weekly refresh job (instagram_token_service), surfaced by /health.
Same single-instance caveat as the rate limiter / realtime hub.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_IG_WARN_INTERVAL_SECONDS = 300

_ig_disabled_reason: str | None = None
_ig_last_warned_at: float | None = None
_ig_client_status: dict[int, str] = {}


def disable_instagram(reason: str) -> None:
    """Mark the Instagram channel disabled for this process, remembering why."""
    global _ig_disabled_reason, _ig_last_warned_at
    _ig_disabled_reason = reason
    _ig_last_warned_at = None


def enable_instagram() -> None:
    """Clear the Instagram-disabled flag."""
    global _ig_disabled_reason, _ig_last_warned_at
    _ig_disabled_reason = None
    _ig_last_warned_at = None


def set_instagram_client_status(client_id: int, verdict: str) -> None:
    """Record the latest token verdict ("valid" | "invalid" | "unknown") for one client's Instagram."""
    _ig_client_status[client_id] = verdict


def instagram_invalid_clients() -> list[int]:
    """client_ids whose Instagram token was last found invalid, sorted."""
    return sorted(cid for cid, v in _ig_client_status.items() if v == "invalid")


def instagram_client_status_summary() -> dict[str, int]:
    """Count of clients per verdict, e.g. {"valid": 3, "invalid": 1}."""
    summary: dict[str, int] = {}
    for verdict in _ig_client_status.values():
        summary[verdict] = summary.get(verdict, 0) + 1
    return summary


def clear_instagram_client_status() -> None:
    """Forget all per-client verdicts (used by tests)."""
    _ig_client_status.clear()


def is_instagram_disabled() -> bool:
    """True when the Instagram channel kill switch is on."""
    return _ig_disabled_reason is not None


def instagram_disabled_reason() -> str | None:
    """Why Instagram is disabled, or None when it is not."""
    return _ig_disabled_reason


def warn_instagram_send_skipped() -> None:
    """Log that an IG send was skipped, at most once per _IG_WARN_INTERVAL_SECONDS."""
    global _ig_last_warned_at
    now = time.monotonic()
    if _ig_last_warned_at is not None and now - _ig_last_warned_at < _IG_WARN_INTERVAL_SECONDS:
        return
    _ig_last_warned_at = now
    logger.warning(
        "Instagram send skipped — channel disabled (%s). Reconnect Instagram and restart "
        "to re-enable. This warning is rate-limited to once per %ds.",
        _ig_disabled_reason,
        _IG_WARN_INTERVAL_SECONDS,
    )
