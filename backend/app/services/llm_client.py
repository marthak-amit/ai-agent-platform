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
- `llm_call()` / `llm_transcribe()` are THE entry points for application code: they
  time the call and record real token usage, cost, latency and outcome to the
  `llm_usage` table (app/services/llm_usage_service.py). Nothing outside this
  module may touch a provider client directly (CI guard: tests/test_llm_usage.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

from groq import Groq
from openai import AsyncOpenAI

from app.config import get_settings
from app.services import llm_health, llm_usage_service
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


JSON_MODE_HINT = "Respond only with a valid JSON object."


def _mentions_json(messages: list[dict]) -> bool:
    """True when any message's text contains the word 'json' (what Groq/OpenAI json_object mode requires)."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # multimodal parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and "json" in content.lower():
            return True
    return False


def ensure_json_instruction(messages: list[dict], response_format: dict | None) -> list[dict]:
    """
    Guarantee a `json_object` request mentions JSON, else the provider rejects it with HTTP 400
    ("'messages' must contain the word 'json' ..."). Backstop for prompts that were truncated
    or never said it; appends JSON_MODE_HINT to the system message. Returns `messages` as-is
    when no JSON mode is requested or the word is already present (input is never mutated).
    """
    if not response_format or response_format.get("type") != "json_object" or _mentions_json(messages):
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = f"{m['content']}\n\n{JSON_MODE_HINT}"
            return out
    return [{"role": "system", "content": JSON_MODE_HINT}, *out]


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
    messages = ensure_json_instruction(messages, response_format)
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


def _tokens(usage: Any, field: str) -> int:
    """Read an int token count from a response.usage object; 0 if absent or not an int."""
    value = getattr(usage, field, 0)
    return value if isinstance(value, int) else 0


async def llm_call(
    purpose: str,
    model: str,
    messages: list[dict],
    client_id: int | None = None,
    conversation_id: int | None = None,
    order_id: int | None = None,
    *,
    max_tokens: int,
    temperature: float = 0.0,
    response_format: dict | None = None,
    client: AsyncOpenAI | None = None,
    use_breaker: bool = True,
):
    """
    The one chat-completion entry point: call the model, then record the outcome.

    Records purpose, model, response.usage tokens, ₹ cost, latency and success/error_code
    to `llm_usage` — for failures too (then re-raises). client_id/conversation_id default
    to the ambient context set by the pipeline. A breaker short-circuit
    (LLMUnavailableError) made no provider call, so it is not recorded.
    `use_breaker=False` keeps a side feature (vision) from tripping the reply-path breaker.
    """
    t0 = time.perf_counter()
    try:
        if use_breaker:
            resp = await chat(
                model, messages, max_tokens=max_tokens, temperature=temperature,
                response_format=response_format, client=client,
            )
        else:
            kwargs = build_request(
                model, messages, max_tokens=max_tokens, temperature=temperature, response_format=response_format
            )
            resp = await (client or get_client()).chat.completions.create(**kwargs)
    except Exception as exc:
        if not isinstance(exc, llm_health.LLMUnavailableError):
            await llm_usage_service.record_usage(
                purpose=purpose, model=model, latency_ms=int((time.perf_counter() - t0) * 1000), success=False,
                error_code=llm_usage_service.error_code_for(exc),
                client_id=client_id, conversation_id=conversation_id, order_id=order_id,
            )
        raise
    usage = getattr(resp, "usage", None)
    await llm_usage_service.record_usage(
        purpose=purpose, model=model,
        prompt_tokens=_tokens(usage, "prompt_tokens"), completion_tokens=_tokens(usage, "completion_tokens"),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        client_id=client_id, conversation_id=conversation_id, order_id=order_id,
    )
    return resp


def get_stt_client() -> Groq:
    """Groq SDK client for Whisper transcription (the only non-OpenAI-compatible Groq call)."""
    return Groq(api_key=get_settings().groq_api_key)


async def llm_transcribe(
    purpose: str,
    model: str,
    audio_bytes: bytes,
    filename: str,
    mime_type: str,
    client_id: int | None = None,
    conversation_id: int | None = None,
    order_id: int | None = None,
    *,
    language: str | None = None,
    prompt: str | None = None,
    client: Groq | None = None,
) -> str:
    """
    Speech-to-text through Groq Whisper, recorded like any other call.

    Whisper has no token usage, so tokens are 0 and cost is an estimate from the audio size
    (per-hour pricing with a 10 s minimum). The blocking SDK call runs in a worker thread.
    """
    t0 = time.perf_counter()
    stt = client or get_stt_client()
    extra = {k: v for k, v in (("language", language), ("prompt", prompt)) if v}
    try:
        result = await asyncio.to_thread(
            stt.audio.transcriptions.create,
            file=(filename, audio_bytes, mime_type), model=model, response_format="text", **extra,
        )
    except Exception as exc:
        await llm_usage_service.record_usage(
            purpose=purpose, model=model, latency_ms=int((time.perf_counter() - t0) * 1000), success=False,
            error_code=llm_usage_service.error_code_for(exc),
            client_id=client_id, conversation_id=conversation_id, order_id=order_id, cost_inr=0.0,
        )
        raise
    await llm_usage_service.record_usage(
        purpose=purpose, model=model, latency_ms=int((time.perf_counter() - t0) * 1000),
        client_id=client_id, conversation_id=conversation_id, order_id=order_id,
        cost_inr=llm_usage_service.stt_cost_inr(len(audio_bytes)),
    )
    return result if isinstance(result, str) else str(result)


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
    purpose: str = "json",
    client_id: int | None = None,
    conversation_id: int | None = None,
    order_id: int | None = None,
    validate: Callable[[dict], bool] | None = None,
    on_response: Callable[[Any], None] | None = None,
    retries: int = 1,
) -> dict | None:
    """
    Chat in JSON mode and return a validated dict, or None.

    Retries (default once) with a repair instruction when the output isn't a
    JSON object or fails `validate`; every attempt is its own `llm_usage` row.
    `on_response` sees every raw response. API errors propagate to the caller.
    """
    msgs = list(messages)
    for attempt in range(retries + 1):
        resp = await llm_call(
            purpose, model, msgs, client_id, conversation_id, order_id,
            max_tokens=max_tokens, response_format={"type": "json_object"},
        )
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
