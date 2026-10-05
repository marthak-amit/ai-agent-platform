"""
Runtime channel-health flags (in-process, reset on restart).

Currently tracks only Instagram: when the startup token check finds the
Instagram token invalid, the channel is marked disabled so IG sends are
skipped with a clear (rate-limited) warning instead of failing on every
attempt. Same single-instance caveat as the rate limiter / realtime hub.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_IG_WARN_INTERVAL_SECONDS = 300

_ig_disabled_reason: str | None = None
_ig_last_warned_at: float | None = None


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


def is_instagram_disabled() -> bool:
    """True when the startup check marked the Instagram token invalid."""
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
