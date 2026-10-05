"""
LLM runtime health — failure counter, circuit breaker, startup model check.

Why this exists: the classifiers (classify_buy_intent, is_off_topic_message,
classify_user_intent, ...) deliberately fall back to a safe default on any
error. When the model name is wrong (Groq 404 model_not_found) that made every
call "succeed" with the default and the bot silently degraded. Now every
fallback is counted and logged at ERROR (`record_failure`), the count is
exposed on /health (`llm_failures_last_hour`), and a circuit breaker routes
around the LLM reply path entirely while the LLM is down.

State is in-process (single instance, same caveat as the rate limiter).
"""

from __future__ import annotations

import logging
import time
from collections import deque

import httpx

logger = logging.getLogger(__name__)

_HOUR = 3600
_ERROR_TEXT_LIMIT = 200

_failure_times: deque[float] = deque()
_consecutive_failures = 0
_tripped_reason: str | None = None
_last_probe_at = 0.0
_missing_models: dict[str, str] = {}
_vision_disabled_reason: str | None = None
_vision_warned = False


class LLMUnavailableError(RuntimeError):
    """The LLM is down/misconfigured — callers must use a deterministic fallback."""


def reset_state() -> None:
    """Clear all counters/flags (used by tests and after a config reload)."""
    global _consecutive_failures, _tripped_reason, _last_probe_at, _vision_disabled_reason, _vision_warned
    _failure_times.clear()
    _missing_models.clear()
    _consecutive_failures = 0
    _tripped_reason = None
    _last_probe_at = 0.0
    _vision_disabled_reason = None
    _vision_warned = False


# ── failure counter ──────────────────────────────────────────────────────────

def failures_last_hour() -> int:
    """Number of LLM failures (classifier/reply/vision fallbacks) in the last hour."""
    cutoff = time.time() - _HOUR
    while _failure_times and _failure_times[0] < cutoff:
        _failure_times.popleft()
    return len(_failure_times)


def record_failure(kind: str, exc: BaseException | str | None = None) -> None:
    """
    Count one LLM failure and log it at ERROR.

    Call this wherever a wrapper falls back to a safe default. A breaker
    short-circuit (LLMUnavailableError) is not a new failure — it is only
    logged at DEBUG so an outage doesn't flood the log or inflate the count.
    """
    if isinstance(exc, LLMUnavailableError):
        logger.debug("LLM call skipped (%s): %s", kind, exc)
        return
    _failure_times.append(time.time())
    detail = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else (exc or "")
    logger.error(
        "LLM_FAILURE kind=%s failures_last_hour=%d detail=%s",
        kind, failures_last_hour(), str(detail)[:_ERROR_TEXT_LIMIT],
    )


# ── circuit breaker ──────────────────────────────────────────────────────────

def is_rate_limited(exc: BaseException) -> bool:
    """True for HTTP 429 / rate-limit errors — these never trip the breaker."""
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc)


def is_llm_error(exc: BaseException) -> bool:
    """True for errors from the LLM layer (breaker skip or an OpenAI/Groq API/connection error)."""
    if isinstance(exc, LLMUnavailableError):
        return True
    try:
        import openai

        return isinstance(exc, openai.OpenAIError)
    except Exception:  # pragma: no cover - openai always installed
        return False


def note_call_result(ok: bool, exc: BaseException | None = None) -> None:
    """Feed one LLM call outcome to the breaker (success closes it; N hard failures open it)."""
    global _consecutive_failures, _tripped_reason
    from app.config import get_settings

    if ok:
        if _tripped_reason is not None:
            logger.warning("LLM recovered — closing circuit breaker (was: %s)", _tripped_reason)
        _consecutive_failures = 0
        _tripped_reason = None
        return
    if exc is not None and is_rate_limited(exc):
        return
    _consecutive_failures += 1
    if _tripped_reason is None and _consecutive_failures >= get_settings().llm_breaker_threshold:
        _tripped_reason = f"{_consecutive_failures} consecutive LLM failures (last: {type(exc).__name__ if exc else 'unknown'})"
        logger.critical("LLM marked UNAVAILABLE — %s. Reply path bypassed until a probe succeeds.", _tripped_reason)


def trip(reason: str) -> None:
    """Open the breaker immediately (e.g. configured model missing at startup)."""
    global _tripped_reason
    _tripped_reason = reason


def is_llm_available() -> bool:
    """False while the breaker is open."""
    return _tripped_reason is None


def unavailable_reason() -> str | None:
    """Why the LLM is considered unavailable, or None."""
    return _tripped_reason


def should_attempt_llm() -> bool:
    """True when the LLM is up, or when the breaker is open but a periodic probe is due."""
    global _last_probe_at
    if _tripped_reason is None:
        return True
    from app.config import get_settings

    now = time.monotonic()
    if now - _last_probe_at >= get_settings().llm_probe_interval_seconds:
        _last_probe_at = now
        return True
    return False


# ── vision ───────────────────────────────────────────────────────────────────

def is_vision_enabled() -> bool:
    """Vision runs only when a model is configured and it passed the startup check."""
    from app.config import get_settings

    return bool(get_settings().llm_model_vision) and _vision_disabled_reason is None


def vision_disabled_reason() -> str | None:
    """Why vision is off ('no model configured' / startup-check finding), or None."""
    from app.config import get_settings

    if not get_settings().llm_model_vision:
        return "LLM_MODEL_VISION is empty"
    return _vision_disabled_reason


def warn_vision_disabled_once() -> None:
    """Log (once per process) that image matching is skipped because vision is disabled."""
    global _vision_warned
    if not _vision_warned:
        _vision_warned = True
        logger.warning("Vision disabled (%s) — customer images fall back to text-only handling.", vision_disabled_reason())


# ── startup model verification ───────────────────────────────────────────────

def configured_models(settings) -> dict[str, str]:
    """Map role → model id for every configured Groq model (empty vision is omitted)."""
    models = {
        "reply": settings.llm_model_reply,
        "classifier": settings.llm_model_classifier,
        "stt": settings.llm_model_stt,
    }
    if settings.llm_model_vision:
        models["vision"] = settings.llm_model_vision
    for i, name in enumerate(parse_csv(settings.llm_model_reply_fallbacks), start=1):
        models[f"reply_fallback_{i}"] = name
    return models


def parse_csv(value: str) -> list[str]:
    """Split a comma-separated env value into trimmed, non-empty items."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def find_missing_models(configured: dict[str, str], available: list[str]) -> dict[str, str]:
    """Return {role: model} for every configured model absent from `available`."""
    have = set(available)
    return {role: name for role, name in configured.items() if name not in have}


async def fetch_available_models(api_key: str, base_url: str, timeout: float = 10.0) -> list[str]:
    """GET {base_url}/models and return the model ids. Raises on HTTP/network errors."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []) if "id" in m)


async def verify_models(settings) -> None:
    """
    Startup check: confirm every configured model exists in Groq's /models.

    Missing reply/classifier model → CRITICAL + breaker opened (health shows
    "LLM unavailable"); missing vision → CRITICAL + vision disabled; missing STT
    → CRITICAL. An unreachable /models endpoint is inconclusive (WARNING only).
    Never raises.
    """
    global _vision_disabled_reason
    if not settings.groq_api_key:
        trip("GROQ_API_KEY is not set")
        logger.critical("GROQ_API_KEY is not set — all LLM calls will fail; bot will use template fallbacks.")
        return
    try:
        available = await fetch_available_models(settings.groq_api_key, settings.groq_base_url)
    except Exception as exc:
        logger.warning("LLM model check skipped — could not list models (%s)", type(exc).__name__)
        return

    missing = find_missing_models(configured_models(settings), available)
    _missing_models.clear()
    _missing_models.update(missing)
    if not missing:
        logger.info("LLM models OK: %s", configured_models(settings))
        return

    logger.critical(
        "LLM model(s) NOT FOUND on Groq: %s. Set LLM_MODEL_* env vars to ids from "
        "`python scripts/check_llm_models.py`. Available (first 20): %s",
        missing, available[:20],
    )
    if "reply" in missing or "classifier" in missing:
        trip(f"configured model missing: { {r: m for r, m in missing.items() if r in ('reply', 'classifier')} }")
    if "vision" in missing:
        _vision_disabled_reason = f"model {missing['vision']!r} not found on Groq"


def health_summary() -> dict:
    """Snapshot for /health: availability, reason, failures_last_hour, vision state."""
    return {
        "available": is_llm_available(),
        "reason": unavailable_reason(),
        "failures_last_hour": failures_last_hour(),
        "vision_enabled": is_vision_enabled(),
        "vision_disabled_reason": vision_disabled_reason() if not is_vision_enabled() else None,
        "missing_models": dict(_missing_models),
    }
