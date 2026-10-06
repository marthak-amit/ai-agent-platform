"""
Voice note transcription service using Groq Whisper.

Cost reference: Groq Whisper charges ~$0.04/hour of audio.
A typical WhatsApp voice note is 10–15 seconds → ~$0.0002 per note (~₹0.02).

Model: LLM_MODEL_STT (a Groq Whisper model) — fast, multilingual, handles Hindi/Gujarati/English
well for Indian WhatsApp business use cases.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.services import llm_client

logger = logging.getLogger(__name__)


def _get_groq_client():
    """Return the Groq STT client (constructed in llm_client, the only module that builds one)."""
    return llm_client.get_stt_client()


async def transcribe_voice_note(
    audio_bytes: bytes,
    filename: str = "audio.ogg",
    client_id: int | None = None,
    conversation_id: int | None = None,
) -> str:
    """
    Transcribe a WhatsApp or Instagram voice note using Groq Whisper.

    The audio is sent as a tuple (filename, bytes, mime-type) which is the
    form Groq's Python SDK expects for in-memory uploads.

    Args:
        audio_bytes: Raw audio bytes (OGG/Opus from WhatsApp, MP4/AAC from Instagram).
        filename:    Filename hint including extension — Groq uses this to infer
                     the codec. Defaults to "audio.ogg" for WhatsApp voice notes.
        client_id / conversation_id: Optional llm_usage attribution; default to the
                     pipeline's ambient context.

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
        transcription = await llm_client.llm_transcribe(
            "stt", get_settings().llm_model_stt, audio_bytes, filename, mime_type,
            client_id=client_id, conversation_id=conversation_id,
            client=client,
            language="hi",
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
