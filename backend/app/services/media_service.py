"""
Inbound/outbound chat media storage.

Meta's media URLs expire (WhatsApp media ids resolve to short-lived CDN links),
so every image/audio a customer sends is downloaded once and re-hosted in OUR
storage — R2 when configured, local /uploads/chat/ otherwise (same fallback
convention as storage_service for product images).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from app.config import get_settings
from app.services import storage_service

logger = logging.getLogger(__name__)

_EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "audio/aac": "aac", "audio/amr": "amr",
}

#: Largest image the dashboard may attach to an outbound message (Meta's own cap is 5 MB).
MAX_OUTBOUND_IMAGE_BYTES = 5 * 1024 * 1024


def sniff_content_type(data: bytes, default: str = "application/octet-stream") -> str:
    """Best-effort magic-byte sniff for the image/audio types chat media can be."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"OggS":
        return "audio/ogg"
    return default


def absolute_url(url: str) -> str:
    """
    Make a stored-media URL fetchable by Meta.

    R2 URLs are already absolute; local /uploads/... paths are prefixed with
    settings.backend_public_url (empty in local dev → returned unchanged, which
    Meta simply can't fetch — same limitation invoice links already have).
    """
    if url.startswith(("http://", "https://")):
        return url
    base = (get_settings().backend_public_url or "").rstrip("/")
    return f"{base}{url}" if base else url


async def store_media(client_id: int, data: bytes, content_type: str, folder: str = "chat") -> str:
    """
    Persist raw media bytes and return a URL for them.

    Args:
        client_id:    Owning client (top-level storage prefix).
        data:         Raw bytes.
        content_type: MIME type (determines the file extension).
        folder:       Sub-prefix, e.g. 'chat', 'payment-qr'.

    Returns:
        R2 public URL, or a local '/uploads/{folder}/...' path when R2 is unset.
    """
    ext = _EXT_BY_TYPE.get(content_type.lower(), "bin")
    key = f"{folder}/{client_id}/{uuid.uuid4().hex}.{ext}"

    url = await asyncio.to_thread(storage_service.upload_file, key, data, content_type)
    if url:
        return url

    local_dir = os.path.join(storage_service._UPLOADS_DIR, folder)
    os.makedirs(local_dir, exist_ok=True)
    filename = f"{client_id}_{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(local_dir, filename), "wb") as fh:
        fh.write(data)
    return f"/uploads/{folder}/{filename}"
