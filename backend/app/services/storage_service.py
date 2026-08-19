"""
Cloudflare R2 object storage — product/catalogue image uploads.

R2 is S3-compatible, so this uses boto3's S3 client pointed at the R2
account endpoint. Images are resized/compressed before upload so WhatsApp
and Instagram's CDN fetch stays fast and R2 storage/egress stays cheap.

Falls back to local disk (matching the pre-R2 behaviour) when R2 isn't
configured, so local dev keeps working without R2 credentials.
"""

from __future__ import annotations

import io
import logging
import os
import re
import uuid
from functools import lru_cache
from typing import Optional

import boto3
import httpx
from botocore.exceptions import ClientError
from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH = 1200
JPEG_QUALITY = 80

_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")

# ── SKU watermark ────────────────────────────────────────────────────────────
# Fixed size/position/format so the watermark lands in the same place on every
# outgoing product photo — ocr_service.py crops exactly this bottom-right
# region when reading a screenshot the customer sends back later, so these
# constants must stay in sync with whatever ocr_service crops.
WATERMARK_FONT_SIZE = 28
WATERMARK_MARGIN = 14
WATERMARK_BG_OPACITY = 170  # 0-255, semi-transparent box behind the text
WATERMARK_CROP_FRACTION_W = 0.35  # rightmost 35% of the width
WATERMARK_CROP_FRACTION_H = 0.15  # bottommost 15% of the height


def is_r2_configured() -> bool:
    """Return True if enough R2 settings are present to upload objects."""
    settings = get_settings()
    return bool(
        settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key
        and settings.r2_bucket_name
    )


@lru_cache
def _r2_client():
    """Return a cached boto3 S3-compatible client configured for R2."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def compress_image(contents: bytes) -> bytes:
    """
    Downscale to a max width of 1200px (upscaling never happens) and
    re-encode as JPEG at quality 80.

    Args:
        contents: Raw uploaded image bytes (any Pillow-readable format).

    Returns:
        Re-encoded JPEG bytes.

    Raises:
        PIL.UnidentifiedImageError: If contents isn't a readable image.
    """
    image = Image.open(io.BytesIO(contents))
    if image.mode != "RGB":
        image = image.convert("RGB")

    if image.width > MAX_IMAGE_WIDTH:
        new_height = round(image.height * (MAX_IMAGE_WIDTH / image.width))
        image = image.resize((MAX_IMAGE_WIDTH, new_height), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _watermark_font():
    """Bundled scalable Pillow font — no filesystem font install needed on Railway."""
    try:
        return ImageFont.load_default(size=WATERMARK_FONT_SIZE)
    except TypeError:
        # Pillow < 10.1 — load_default() doesn't accept a size argument.
        return ImageFont.load_default()


def watermark_sku(contents: bytes, sku: str) -> bytes:
    """
    Draw a "SKU: {sku}" watermark bottom-right on a COPY of the image, over
    a semi-transparent background box so it stays legible against any photo.

    Font size, margin, and corner are fixed constants (WATERMARK_*) so the
    watermark lands in the exact same place on every product photo —
    ocr_service.py depends on this consistency to crop the right region
    when reading it back from a customer's re-shared screenshot.

    Does not touch the original — returns brand-new JPEG bytes.

    Args:
        contents: Source image bytes (any Pillow-readable format).
        sku:      SKU string to render, e.g. "PR10983".

    Returns:
        Watermarked JPEG bytes.

    Raises:
        PIL.UnidentifiedImageError: If contents isn't a readable image.
    """
    image = Image.open(io.BytesIO(contents))
    if image.mode != "RGB":
        image = image.convert("RGB")
    base = image.convert("RGBA")

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _watermark_font()
    label = f"SKU: {sku.strip().upper()}"

    text_bbox = draw.textbbox((0, 0), label, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]

    box_x2 = base.width - WATERMARK_MARGIN
    box_y2 = base.height - WATERMARK_MARGIN
    box_x1 = box_x2 - text_w - WATERMARK_MARGIN * 2
    box_y1 = box_y2 - text_h - WATERMARK_MARGIN * 2
    draw.rectangle([box_x1, box_y1, box_x2, box_y2], fill=(0, 0, 0, WATERMARK_BG_OPACITY))
    draw.text(
        (box_x1 + WATERMARK_MARGIN - text_bbox[0], box_y1 + WATERMARK_MARGIN - text_bbox[1]),
        label, font=font, fill=(255, 255, 255, 255),
    )

    watermarked = Image.alpha_composite(base, overlay).convert("RGB")
    buffer = io.BytesIO()
    watermarked.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _watermark_key(client_id: int, sku: str) -> str:
    """Deterministic R2 key for a SKU's watermarked copy — same key reused across sends."""
    safe_sku = re.sub(r"[^A-Za-z0-9_-]", "_", sku.strip().upper()) or "UNKNOWN"
    return f"{client_id}/watermarked/{safe_sku}.jpg"


async def get_watermarked_image_url(
    client_id: int, sku: str, source_image_url: str
) -> Optional[str]:
    """
    Return a public URL for a copy of `source_image_url` watermarked with `sku`.

    Uses a deterministic key (per client+SKU) so the first send for a product
    generates and uploads the watermarked copy, and every later send of the
    same product reuses that same object — no repeated download/watermark/
    upload round trip on every message.

    Never raises — returns None if the source image can't be fetched or
    isn't readable, so callers can fall back to sending the unwatermarked
    original rather than failing the whole send.

    Args:
        client_id:        Owning client's ID (storage prefix + cache key).
        sku:               Product SKU to render into the watermark.
        source_image_url:  Public URL of the original product photo (the
                            same URL that would otherwise be sent as-is).

    Returns:
        A publicly fetchable URL to the watermarked copy, or None on failure.
    """
    settings = get_settings()

    if is_r2_configured():
        key = _watermark_key(client_id, sku)
        try:
            _r2_client().head_object(Bucket=settings.r2_bucket_name, Key=key)
            if settings.r2_public_base_url:
                return f"{settings.r2_public_base_url.rstrip('/')}/{key}"
        except ClientError:
            pass  # not generated yet — fall through and create it
        except Exception as exc:
            logger.warning("Watermark cache lookup failed for %s: %s", key, exc)

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.get(source_image_url)
            response.raise_for_status()
            original_bytes = response.content
    except Exception as exc:
        logger.warning("Could not fetch source image for watermarking (%s): %s", source_image_url, exc)
        return None

    try:
        watermarked = watermark_sku(original_bytes, sku)
    except Exception as exc:
        logger.warning("Watermarking failed for sku=%s: %s", sku, exc)
        return None

    if not is_r2_configured():
        filename = f"wm_{client_id}_{uuid.uuid4().hex[:12]}.jpg"
        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        with open(os.path.join(_UPLOADS_DIR, filename), "wb") as f:
            f.write(watermarked)
        return f"/uploads/{filename}"

    key = _watermark_key(client_id, sku)
    _r2_client().put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=watermarked,
        ContentType="image/jpeg",
    )
    if settings.r2_public_base_url:
        return f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    logger.warning(
        "Watermarked image uploaded to R2 key %s but R2_PUBLIC_BASE_URL is unset "
        "— it won't be fetchable by WhatsApp/Instagram.",
        key,
    )
    return f"{settings.r2_endpoint}/{settings.r2_bucket_name}/{key}"


def upload_file(key: str, data: bytes, content_type: str) -> Optional[str]:
    """
    Upload raw bytes to R2 under an explicit key and return the public URL.

    For non-image assets (e.g. invoice PDFs) that don't need the
    compress/watermark treatment the product-image helpers above apply.

    Returns:
        Publicly fetchable URL, or None if R2 isn't configured (or its
        public base URL isn't set) — callers should fall back to local disk.
    """
    if not is_r2_configured():
        return None

    settings = get_settings()
    _r2_client().put_object(
        Bucket=settings.r2_bucket_name, Key=key, Body=data, ContentType=content_type,
    )
    if settings.r2_public_base_url:
        return f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    logger.warning(
        "Uploaded to R2 key %s but R2_PUBLIC_BASE_URL is unset — it won't be fetchable.",
        key,
    )
    return None


def _build_key(client_id: int, product_id: Optional[int]) -> str:
    """Build the R2 object key: {client_id}/{product_id or 'unassigned'}/{uuid}.jpg."""
    scope = str(product_id) if product_id is not None else "unassigned"
    return f"{client_id}/{scope}/{uuid.uuid4().hex[:12]}.jpg"


def upload_product_image(
    client_id: int,
    contents: bytes,
    product_id: Optional[int] = None,
) -> str:
    """
    Compress an uploaded product image and store it, returning a public URL.

    Uploads to R2 when configured (settings.r2_public_base_url set); falls
    back to local disk served under /uploads/ otherwise, so local dev works
    without R2 credentials.

    Args:
        client_id:  Owning client's ID — used as the top-level storage prefix.
        contents:   Raw uploaded image bytes.
        product_id: Parent product ID if known (None for new, not-yet-saved
                    products — stored under an "unassigned" prefix instead).

    Returns:
        Publicly fetchable image URL.

    Raises:
        PIL.UnidentifiedImageError: If contents isn't a readable image.
    """
    compressed = compress_image(contents)
    settings = get_settings()

    if is_r2_configured():
        key = _build_key(client_id, product_id)
        _r2_client().put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=compressed,
            ContentType="image/jpeg",
        )
        if settings.r2_public_base_url:
            url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
            logger.info("Image uploaded to R2: %s (client %d)", key, client_id)
            return url
        logger.warning(
            "R2 upload succeeded for key %s but R2_PUBLIC_BASE_URL is unset — "
            "enable public access on the bucket in the Cloudflare dashboard "
            "and set R2_PUBLIC_BASE_URL, or the image won't be fetchable by "
            "WhatsApp/Instagram.",
            key,
        )
        return f"{settings.r2_endpoint}/{settings.r2_bucket_name}/{key}"

    # Local-disk fallback (pre-R2 behaviour) — dev environments without R2 creds.
    filename = f"{client_id}_{uuid.uuid4().hex[:12]}.jpg"
    os.makedirs(_UPLOADS_DIR, exist_ok=True)
    with open(os.path.join(_UPLOADS_DIR, filename), "wb") as f:
        f.write(compressed)
    return f"/uploads/{filename}"
