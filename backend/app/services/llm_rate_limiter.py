"""
Client-side token bucket per model — avoid Groq's 429 instead of suffering it (conv=60: planner prompts of ~4.8k tokens against an 8k tokens-per-minute cap).

Every call ``acquire``s its estimated tokens first. The limiter keeps a sliding 60 s window of tokens and requests per model against the
configured limits (``LLM_RATE_LIMITS``, JSON ``{"model": {"tpm": n, "rpm": n}}``; a model with no entry is unlimited). When there is no room
the call may queue for ``llm_rate_limit_wait_seconds`` (default 2 s) for capacity to free up; past that ``acquire`` returns ``None`` and the
caller goes to the fallback model. After the call the estimate is replaced by the real ``usage`` (``settle``), and the provider's own
``x-ratelimit-*`` response headers (``observe_headers``) tighten the picture: ``remaining-tokens`` below the estimate blocks the model until
``reset-tokens``; a 429 blocks it for ``retry-after``.

In-process like the rate limiter of the webhook: one Railway instance. With several instances each keeps its own window — set each instance's
limits to its share, or move the counters to Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0
_POLL = 0.05
_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_duration(value: str | None) -> float | None:
    """'2.5s' → 2.5 · '1m26.4s' → 86.4 · '250ms' → 0.25 · '7' → 7.0 (seconds); None when unreadable."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    m = _DURATION_RE.match(text)
    if not m or not any(m.groups()):
        return None
    h, mi, sec, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + sec + ms / 1000.0


@dataclass
class Entry:
    """Tokens one call reserved (replaced by the real usage once known)."""

    at: float
    tokens: int


@dataclass
class ModelState:
    """The sliding window and the provider's last word for one model."""

    entries: list[Entry] = field(default_factory=list)
    remaining_tokens: int | None = None
    remaining_until: float = 0.0          # monotonic time the header-reported `remaining_tokens` stops being true (the bucket refills)
    blocked_until: float = 0.0            # a 429 / zero remaining: no calls before this


_states: dict[str, ModelState] = {}


def reset() -> None:
    """Forget everything (tests)."""
    _states.clear()


def _state(model: str) -> ModelState:
    """The state of `model` (created on first use)."""
    return _states.setdefault(model, ModelState())


def limits_for(model: str) -> tuple[int, int]:
    """(tokens per minute, requests per minute) configured for `model`; 0 = unlimited."""
    from app.config import get_settings

    try:
        table = json.loads(get_settings().llm_rate_limits or "{}")
        row = table.get(model) or {}
        return int(row.get("tpm", 0) or 0), int(row.get("rpm", 0) or 0)
    except (ValueError, TypeError, AttributeError):
        return 0, 0


def _purge(st: ModelState, now: float) -> None:
    """Drop window entries older than a minute."""
    st.entries = [e for e in st.entries if now - e.at < WINDOW_SECONDS]


def used_tokens(model: str, now: float | None = None) -> int:
    """Tokens spent / reserved on `model` in the last minute."""
    now = time.monotonic() if now is None else now
    st = _state(model)
    _purge(st, now)
    return sum(e.tokens for e in st.entries)


def _wait_needed(model: str, est: int, now: float) -> float | None:
    """Seconds until a call of `est` tokens fits (0 = now), or None when it can never fit (bigger than the whole limit)."""
    tpm, rpm = limits_for(model)
    st = _state(model)
    _purge(st, now)
    wait = max(0.0, st.blocked_until - now)
    if st.remaining_tokens is not None and now < st.remaining_until and est > st.remaining_tokens:
        wait = max(wait, st.remaining_until - now)
    if tpm and est > tpm:
        return None
    if tpm:
        over = sum(e.tokens for e in st.entries) + est - tpm
        if over > 0:
            freed = 0
            for e in sorted(st.entries, key=lambda e: e.at):
                freed += e.tokens
                if freed >= over:
                    wait = max(wait, e.at + WINDOW_SECONDS - now)
                    break
    if rpm and len(st.entries) >= rpm:
        oldest = min(e.at for e in st.entries)
        wait = max(wait, oldest + WINDOW_SECONDS - now)
    return wait


async def acquire(model: str, est_tokens: int, *, max_wait: float | None = None) -> Entry | None:
    """
    Reserve `est_tokens` on `model`. Returns the reservation (pass it to ``settle``) or None when there is no room within `max_wait` seconds
    (default ``llm_rate_limit_wait_seconds``) — the caller then uses the fallback model.
    """
    from app.config import get_settings

    if max_wait is None:
        max_wait = float(get_settings().llm_rate_limit_wait_seconds)
    deadline = time.monotonic() + max_wait
    while True:
        now = time.monotonic()
        wait = _wait_needed(model, est_tokens, now)
        if wait is None:
            return Entry(now, est_tokens)          # larger than the whole limit: let the provider decide (and the 429 path handle it)
        if wait <= 0:
            entry = Entry(now, est_tokens)
            _state(model).entries.append(entry)
            return entry
        if now + wait > deadline:
            logger.info("RATE_LIMIT model=%s est=%d used=%d wait=%.1fs > %.1fs — not queued", model, est_tokens, used_tokens(model, now), wait, max_wait)
            return None
        await asyncio.sleep(min(wait, max(_POLL, deadline - now), 0.5))


def settle(model: str, entry: Entry | None, actual_tokens: int | None) -> None:
    """Replace a reservation's estimate by the tokens the provider really counted (None = the call failed: give the reservation back)."""
    if entry is None:
        return
    st = _state(model)
    if actual_tokens is None:
        st.entries = [e for e in st.entries if e is not entry]
    else:
        entry.tokens = max(0, int(actual_tokens))


def note_rate_limited(model: str, retry_after: float | None = None) -> None:
    """The provider answered 429: no more calls to this model for `retry_after` seconds (default 5)."""
    st = _state(model)
    st.blocked_until = max(st.blocked_until, time.monotonic() + min(max(retry_after if retry_after is not None else 5.0, 0.5), 60.0))


def observe_headers(model: str | None, headers) -> None:
    """Learn from ``x-ratelimit-remaining-tokens`` / ``-reset-tokens`` / ``retry-after`` on any response (never raises)."""
    if not model or headers is None:
        return
    try:
        get = headers.get
        remaining = get("x-ratelimit-remaining-tokens")
        reset = parse_duration(get("x-ratelimit-reset-tokens"))
        st = _state(model)
        now = time.monotonic()
        if remaining is not None and str(remaining).strip().lstrip("-").isdigit():
            st.remaining_tokens = int(remaining)
            st.remaining_until = now + (reset if reset is not None else WINDOW_SECONDS)
        retry_after = parse_duration(get("retry-after"))
        if retry_after is not None:
            note_rate_limited(model, retry_after)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rate-limit headers unreadable: %s", exc)


def estimate_tokens(messages: list[dict], schema_chars: int = 0, completion: int = 700) -> int:
    """A cheap upper-ish estimate of one call's tokens: ~3.4 characters per token of prompt (+ schema) plus the completion budget."""
    chars = schema_chars
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        chars += len(content or "")
    return int(chars / 3.4) + completion


def snapshot() -> dict[str, dict]:
    """Per-model view for /health and logs: tokens used in the last minute, the limit, and any block."""
    now = time.monotonic()
    out = {}
    for model in list(_states):
        tpm, rpm = limits_for(model)
        out[model] = {"used_tokens": used_tokens(model, now), "tpm": tpm, "requests": len(_state(model).entries), "rpm": rpm,
                      "blocked_for": round(max(0.0, _state(model).blocked_until - now), 1)}
    return out
