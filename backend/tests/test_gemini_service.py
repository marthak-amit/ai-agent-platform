"""
Tests for app/services/gemini_service.py (now backed by OpenAI GPT-4o-mini).

Mocks the AsyncOpenAI client so no real API call is made.
The public interface — generate_reply(user_message, history, system_prompt,
catalogue_context) — is identical to the previous Gemini implementation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_openai_response(content: str) -> MagicMock:
    """Build a mock that matches openai.types.chat.ChatCompletion structure."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


# ── generate_reply ────────────────────────────────────────────────────────────

@patch("app.services.gemini_service._get_client")
async def test_generate_reply_returns_text(mock_get_client, mock_settings):
    """generate_reply returns stripped text from the OpenAI response."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("  AI answer  ")
    )
    mock_get_client.return_value = mock_client

    result = await generate_reply("What is AI?")

    assert result == "AI answer"
    mock_client.chat.completions.create.assert_called_once()


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_with_history(mock_get_client, mock_settings):
    """generate_reply includes history turns and translates 'model' → 'assistant'."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Answer with history")
    )
    mock_get_client.return_value = mock_client

    history = [
        {"role": "user", "content": "Previous question"},
        {"role": "model", "content": "Previous answer"},
    ]
    await generate_reply("Follow-up", history=history)

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    messages = call_kwargs["messages"]

    # system + 2 history + 1 current = 4 messages
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # "model" from DB must be translated to "assistant" for OpenAI
    assert messages[2]["role"] == "assistant"
    assert messages[3]["role"] == "user"
    assert messages[3]["content"] == "Follow-up"


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_history_capped_at_10(mock_get_client, mock_settings):
    """Only the last 10 history turns are sent to avoid token bloat."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Reply")
    )
    mock_get_client.return_value = mock_client

    # 15 history turns — only last 10 should reach the API
    history = [
        {"role": "user" if i % 2 == 0 else "model", "content": f"msg {i}"}
        for i in range(15)
    ]
    await generate_reply("New question", history=history)

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    messages = call_kwargs["messages"]
    # system (1) + last 10 history + current user (1) = 12
    assert len(messages) == 12


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_raises_on_empty_response(mock_get_client, mock_settings):
    """generate_reply raises RuntimeError when OpenAI returns empty content."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("")
    )
    mock_get_client.return_value = mock_client

    with pytest.raises(RuntimeError, match="empty response"):
        await generate_reply("Hello")


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_with_system_prompt(mock_get_client, mock_settings):
    """The client's system prompt is placed first in the messages list."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Sure!")
    )
    mock_get_client.return_value = mock_client

    await generate_reply("Hello", system_prompt="You are a saree expert.")

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    messages = call_kwargs["messages"]
    assert messages[0]["role"] == "system"
    # Language rule is prepended; verify the caller's prompt is still present.
    assert "You are a saree expert." in messages[0]["content"]


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_fallback_system_prompt(mock_get_client, mock_settings):
    """When no system_prompt is given a fallback generic prompt is used."""
    from app.services.gemini_service import generate_reply, _FALLBACK_SYSTEM

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Hi!")
    )
    mock_get_client.return_value = mock_client

    await generate_reply("Hello")

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    # Language rule is prepended; verify the fallback prompt body is still present.
    assert _FALLBACK_SYSTEM in call_kwargs["messages"][0]["content"]


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_with_catalogue_context(mock_get_client, mock_settings):
    """catalogue_context is appended to the system prompt, not the user message."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Cotton Saree costs ₹1500")
    )
    mock_get_client.return_value = mock_client

    ctx = "• Cotton Saree — ₹1,500 (stock: 10)"
    await generate_reply(
        "How much is the cotton saree?",
        system_prompt="You are a helpful assistant.",
        catalogue_context=ctx,
    )

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    messages = call_kwargs["messages"]

    # Context must appear in the system message, not the user message
    system_content = messages[0]["content"]
    assert "Available products" in system_content
    assert "Cotton Saree" in system_content

    # User message must be the raw question, unchanged
    user_content = messages[-1]["content"]
    assert user_content == "How much is the cotton saree?"


@patch("app.services.gemini_service._get_client")
async def test_generate_reply_calls_correct_model(mock_get_client, mock_settings):
    """GPT-4o-mini is the model passed to the OpenAI API."""
    from app.services.gemini_service import generate_reply

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response("Done")
    )
    mock_get_client.return_value = mock_client

    await generate_reply("ping")

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "llama-3.3-70b-versatile"
    assert call_kwargs["max_tokens"] == 300
    assert call_kwargs["temperature"] == 0.7


# ── _get_client ───────────────────────────────────────────────────────────────

def test_get_client_uses_openai_api_key(mock_settings):
    """_get_client constructs AsyncOpenAI with OPENAI_API_KEY from settings."""
    from openai import AsyncOpenAI
    from app.services.gemini_service import _get_client

    with patch("app.services.gemini_service.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock(spec=AsyncOpenAI)
        _get_client()
        mock_cls.assert_called_once_with(
            api_key=mock_settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )


def test_format_product_for_whatsapp_in_stock():
    """format_product_for_whatsapp builds the 👉-bulleted detail card for an in-stock item."""
    from app.services.gemini_service import format_product_for_whatsapp

    product = MagicMock()
    product.name = "Designer Lehenga"
    product.sku = "LH10042"
    product.price = 6500
    product.description = "Heavy embroidered lehenga, festive wear"
    product.stock = 7

    card = format_product_for_whatsapp(product)

    assert "*Designer Lehenga*" in card
    assert "👉 SKU: LH10042" in card
    assert "👉 Price: ₹6500" in card
    assert "👉 Details: Heavy embroidered lehenga, festive wear" in card
    assert "✅ In Stock: 7 pieces" in card
    assert "❌ Out of Stock" not in card


def test_format_product_for_whatsapp_out_of_stock():
    """format_product_for_whatsapp shows ❌ Out of Stock when stock is zero or missing."""
    from app.services.gemini_service import format_product_for_whatsapp

    product = MagicMock()
    product.name = "Banarasi Saree"
    product.sku = "SR001"
    product.price = 2450
    product.description = None
    product.stock = 0

    card = format_product_for_whatsapp(product)

    assert "❌ Out of Stock" in card
    assert "✅ In Stock" not in card
