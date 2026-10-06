"""Every json_object LLM request must mention 'json' in its messages (else the provider 400s)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import gemini_service, llm_client, llm_intent, tool_router

JSON_FORMAT = {"type": "json_object"}


def _fake_client(content: str = '{"intent": "CHITCHAT", "calls": []}'):
    """Fake AsyncOpenAI that records the kwargs of every create() call."""
    c = MagicMock()
    resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )
    c.chat.completions.create = AsyncMock(return_value=resp)
    return c


def _assert_json_requests_mention_json(client) -> None:
    """Fail if any recorded json_object request lacks the word 'json' in its messages."""
    assert client.chat.completions.create.await_count >= 1
    for call in client.chat.completions.create.await_args_list:
        kw = call.kwargs
        if (kw.get("response_format") or {}).get("type") == "json_object":
            assert llm_client._mentions_json(kw["messages"]), kw["messages"]


def test_ensure_json_instruction_appends_hint_when_missing():
    """A json_object request with no 'json' gets the hint appended to its system message."""
    msgs = [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}]
    out = llm_client.ensure_json_instruction(msgs, JSON_FORMAT)
    assert llm_client.JSON_MODE_HINT in out[0]["content"]
    assert msgs[0]["content"] == "Be helpful."  # input not mutated


def test_ensure_json_instruction_adds_system_message_when_none():
    """With no system message, a system message carrying the hint is prepended."""
    out = llm_client.ensure_json_instruction([{"role": "user", "content": "hi"}], JSON_FORMAT)
    assert out[0] == {"role": "system", "content": llm_client.JSON_MODE_HINT}


def test_ensure_json_instruction_noop_cases():
    """No change for plain-text requests or when 'JSON' (any case) is already present."""
    plain = [{"role": "user", "content": "hi"}]
    assert llm_client.ensure_json_instruction(plain, None) is plain
    has = [{"role": "user", "content": "give me JSON"}]
    assert llm_client.ensure_json_instruction(has, JSON_FORMAT) is has


def test_build_request_enforces_json_word():
    """build_request is the single choke point: json_object kwargs always mention json."""
    kw = llm_client.build_request(
        "m", [{"role": "user", "content": "hi"}], max_tokens=10, response_format=JSON_FORMAT
    )
    assert llm_client._mentions_json(kw["messages"])


@pytest.mark.asyncio
async def test_chat_json_requests_mention_json():
    """chat_json with a prompt that never says 'json' still sends the word."""
    client = _fake_client('{"a": 1}')
    await llm_client.llm_call(
        "t", "m", [{"role": "user", "content": "hi"}], max_tokens=5, response_format=JSON_FORMAT, client=client,
    )
    _assert_json_requests_mention_json(client)


@pytest.mark.asyncio
async def test_classify_turn_survives_slim_prompt_truncation(monkeypatch):
    """A >8000-char system prompt is truncated for the small fallback model; the JSON instruction must survive."""
    client = _fake_client()
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    monkeypatch.setattr(gemini_service, "_groq_models", lambda: ["big-model", gemini_service.get_settings().llm_model_classifier])
    client.chat.completions.create.side_effect = [Exception("429 rate limited"), client.chat.completions.create.return_value]
    await llm_intent.classify_turn("hi", None, "x" * 9000, "SKU1 shirt", "en")
    _assert_json_requests_mention_json(client)
    last = client.chat.completions.create.await_args_list[-1].kwargs["messages"][0]["content"]
    assert "RESPOND WITH ONLY A JSON OBJECT" in last  # schema itself (not just the hint) survived


@pytest.mark.asyncio
async def test_tool_router_requests_mention_json(monkeypatch):
    """tool_router's chat_json call sends 'json' in its messages."""
    seen = {}

    async def _fake_llm_call(purpose, model, messages, *a, **kw):
        """Capture the post-guard messages the provider would receive."""
        seen["messages"] = llm_client.build_request(model, messages, max_tokens=1, response_format=kw["response_format"])["messages"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"calls": []}'))])

    monkeypatch.setattr(llm_client, "llm_call", _fake_llm_call)
    assert llm_client._mentions_json([{"role": "system", "content": tool_router._SYSTEM_PROMPT}])
    await llm_client.chat_json("m", [{"role": "system", "content": tool_router._SYSTEM_PROMPT}], max_tokens=5)
    assert llm_client._mentions_json(seen["messages"])
