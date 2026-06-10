"""
Tests for app/services/voice_service.py.

Groq client is fully mocked — no real API calls are made.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.voice_service import transcribe_voice_note


@pytest.mark.asyncio
async def test_transcribe_voice_note_success():
    """Returns transcribed text from Groq Whisper."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = "Namaste, saree chahiye"

    with patch("app.services.voice_service._get_groq_client", return_value=mock_client):
        result = await transcribe_voice_note(b"fake_audio", "audio.ogg")

    assert result == "Namaste, saree chahiye"
    mock_client.audio.transcriptions.create.assert_called_once()
    call_kwargs = mock_client.audio.transcriptions.create.call_args.kwargs
    assert call_kwargs["model"] == "whisper-large-v3-turbo"
    assert call_kwargs["language"] == "hi"
    assert call_kwargs["file"][1] == b"fake_audio"
    assert call_kwargs["file"][2] == "audio/ogg"


@pytest.mark.asyncio
async def test_transcribe_voice_note_mp4_mime():
    """Correctly maps .mp4 extension to audio/mp4 MIME type."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = "test"

    with patch("app.services.voice_service._get_groq_client", return_value=mock_client):
        await transcribe_voice_note(b"audio", "audio.mp4")

    call_kwargs = mock_client.audio.transcriptions.create.call_args.kwargs
    assert call_kwargs["file"][2] == "audio/mp4"


@pytest.mark.asyncio
async def test_transcribe_voice_note_unknown_extension():
    """Defaults to audio/ogg for unknown extensions."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = "test"

    with patch("app.services.voice_service._get_groq_client", return_value=mock_client):
        await transcribe_voice_note(b"audio", "audio.xyz")

    call_kwargs = mock_client.audio.transcriptions.create.call_args.kwargs
    assert call_kwargs["file"][2] == "audio/ogg"


@pytest.mark.asyncio
async def test_transcribe_voice_note_strips_whitespace():
    """Strips leading/trailing whitespace from Groq response."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = "  hello world  "

    with patch("app.services.voice_service._get_groq_client", return_value=mock_client):
        result = await transcribe_voice_note(b"audio")

    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_voice_note_graceful_degradation():
    """Returns empty string on Groq API error — never raises."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.side_effect = Exception("Groq down")

    with patch("app.services.voice_service._get_groq_client", return_value=mock_client):
        result = await transcribe_voice_note(b"audio")

    assert result == ""
