"""
Single entry point for Groq chat calls (OpenAI-compatible API).

- Model ids come from config (LLM_MODEL_*) — never hardcode them elsewhere.
- Reasoning models (gpt-oss, ...) get `reasoning_effort` and extra max_tokens
  headroom so hidden reasoning can't starve the visible answer.
- `final_text()` reads ONLY `message.content` (never `reasoning` /
  `reasoning_content`) and strips inline <think> blocks.
- `parse_json_object()` / `chat_json()` tolerate code fences and surrounding
  prose, validate the schema, and retry once on invalid JSON.
- Every call feeds the llm_health circuit breaker.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from openai import AsyncOpenAI

from app.config import get_settings
from app.services import llm_health
from app.services.llm_health import LLMUnavailableError

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def get_client() -> AsyncOpenAI:
    """AsyncOpenAI pointed at Groq. max_retries=0: callers own retry/fallback policy."""
    settings = get_settings()
    return AsyncOpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url, max_retries=0)


def is_reasoning_model(model: str) -> bool:
    """True when `model` matches a configured reasoning-model prefix."""
    prefixes = llm_health.parse_csv(get_settings().llm_reasoning_model_prefixes)
    return any(model.startswith(p) for p in prefixes)


def build_request(
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    response_format: dict | None = None,
) -> dict[str, Any]:
    """Build chat.completions kwargs, adding reasoning settings/headroom for reasoning models."""
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if is_reasoning_model(model):
        kwargs["max_tokens"] = max_tokens + settings.llm_reasoning_headroom_tokens
        if settings.llm_reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": settings.llm_reasoning_effort}
    return kwargs


async def chat(
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    response_format: dict | None = None,
    client: AsyncOpenAI | None = None,
):
    """
    One chat completion, guarded by the circuit breaker.

    Raises LLMUnavailableError immediately (no network call) while the breaker
    is open and no probe is due; otherwise re-raises the API error after
    recording it with llm_health.
    """
    if not llm_health.should_attempt_llm():
        raise LLMUnavailableError(llm_health.unavailable_reason() or "LLM unavailable")
    kwargs = build_request(
        model, messages, max_tokens=max_tokens, temperature=temperature, response_format=response_format
    )
    try:
        resp = await (client or get_client()).chat.completions.create(**kwargs)
    except Exception as exc:
        llm_health.note_call_result(False, exc)
        raise
    llm_health.note_call_result(True)
    return resp


def strip_reasoning(text: str) -> str:
    """Remove inline <think>…</think> blocks some reasoning models put in content."""
    return _THINK_RE.sub("", text or "").strip()


def final_text(resp) -> str:
    """The model's final answer: `choices[0].message.content` only, reasoning stripped ('' if none)."""
    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return strip_reasoning(content) if isinstance(content, str) else ""


def parse_json_object(raw: str | None) -> dict | None:
    """
    Extract a JSON object from model output.

    Handles ```json fences, <think> blocks and leading/trailing prose. Returns
    None unless the result is a JSON *object*.
    """
    text = strip_reasoning(raw or "")
    if not text:
        return None
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


async def chat_json(
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    validate: Callable[[dict], bool] | None = None,
    on_response: Callable[[Any], None] | None = None,
    retries: int = 1,
) -> dict | None:
    """
    Chat in JSON mode and return a validated dict, or None.

    Retries (default once) with a repair instruction when the output isn't a
    JSON object or fails `validate`. `on_response` sees every raw response (for
    cost logging). API errors propagate to the caller.
    """
    msgs = list(messages)
    for attempt in range(retries + 1):
        resp = await chat(model, msgs, max_tokens=max_tokens, response_format={"type": "json_object"})
        if on_response is not None:
            on_response(resp)
        raw = final_text(resp)
        data = parse_json_object(raw)
        if data is not None and (validate is None or validate(data)):
            return data
        logger.warning("chat_json: invalid JSON/schema (attempt %d/%d): %r", attempt + 1, retries + 1, raw[:120])
        msgs = list(messages) + [
            {"role": "assistant", "content": raw or "(empty)"},
            {"role": "user", "content": "That was not valid. Reply with ONLY the JSON object matching the schema."},
        ]
    return None
