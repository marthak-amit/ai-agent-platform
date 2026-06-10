"""
Voice note transcription service using Groq Whisper.

Cost reference: Groq Whisper charges ~$0.04/hour of audio.
A typical WhatsApp voice note is 10–15 seconds → ~$0.0002 per note (~₹0.02).

Model: whisper-large-v3-turbo — fast, multilingual, handles Hindi/Gujarati/English
well for Indian WhatsApp business use cases.
"""

from __future__ import annotations

import logging

from groq import Groq

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_groq_client() -> Groq:
    """Return a Groq client using GROQ_API_KEY from settings."""
    return Groq(api_key=get_settings().groq_api_key)


async def transcribe_voice_note(
    audio_bytes: bytes,
    filename: str = "audio.ogg",
) -> str:
    """
    Transcribe a WhatsApp or Instagram voice note using Groq Whisper.

    The audio is sent as a tuple (filename, bytes, mime-type) which is the
    form Groq's Python SDK expects for in-memory uploads.

    Args:
        audio_bytes: Raw audio bytes (OGG/Opus from WhatsApp, MP4/AAC from Instagram).
        filename:    Filename hint including extension — Groq uses this to infer
                     the codec. Defaults to "audio.ogg" for WhatsApp voice notes.

    Returns:
        Transcribed text as a plain string. Returns a fallback message if
        transcription fails so the conversation can still proceed.

    Raises:
        Does not propagate exceptions — returns a fallback string instead so
        the caller (webhook handler) can degrade gracefully.
    """
    client = _get_groq_client()

    # Infer MIME type from extension
    ext = filename.rsplit(".", 1)[-1].lower()
    mime_map = {
        "ogg": "audio/ogg",
        "oga": "audio/ogg",
        "mp4": "audio/mp4",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
    }
    mime_type = mime_map.get(ext, "audio/ogg")

    try:
        transcription = client.audio.transcriptions.create(
            file=(filename, audio_bytes, mime_type),
            model="whisper-large-v3-turbo",
            language="hi",
            response_format="text",
            prompt=(
                "This is a WhatsApp voice note from an Indian customer asking about "
                "textile products, sarees, prices, or placing orders. "
                "The customer may speak Hindi, Gujarati, or Hinglish."
            ),
        )
        text = transcription if isinstance(transcription, str) else str(transcription)
        logger.info("Voice transcription succeeded (%d chars).", len(text))
        return text.strip()
    except Exception as exc:
        logger.error("Voice transcription failed: %s", exc)
        return ""
