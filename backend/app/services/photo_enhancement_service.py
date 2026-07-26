"""
Product photo enhancement — turns a seller's raw flat-lay variant photo into a
styled dummy/human-model/hanging shot using Gemini 2.5 Flash Image.

Flow per (variant, style) pair:
  1. pre-check gate    — reject low-resolution/blurry raw uploads before spending
  2. Gemini fusion call — raw photo + style reference image -> one generated image
  3. color QC gate      — flag (don't auto-publish) if the garment colour drifted
  4. seller approval    — only an approved 'done' image is ever shown publicly

Nothing here is on the hot path of an order — this is only ever invoked from
the seller's own dashboard action.
"""

from __future__ import annotations

import io
import logging
import os
import uuid
from typing import Optional

import httpx
from google import genai
from google.genai import types
from PIL import Image, ImageFilter, ImageStat
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.photo_generation_log import PhotoGenerationLog
from app.models.product_variant import ProductVariant
from app.models.style_reference import StyleReference
from app.services import billing_service

logger = logging.getLogger(__name__)

GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"

# Pre-check thresholds
MIN_RESOLUTION_PX = 800
# Variance of a Laplacian-like edge map — below this the image is likely blurry.
# Heuristic threshold; tune against real seller uploads if false positives appear.
BLUR_VARIANCE_THRESHOLD = 80.0

# Color QC — normalized RGB histogram intersection (1.0 = identical, 0.0 = disjoint).
# Below this, the generated image's dominant colours drifted too far from the
# raw upload to auto-publish.
COLOR_MISMATCH_THRESHOLD = 0.35

# COGS — Gemini 2.5 Flash Image output + a small buffer for the input tokens
# (raw photo + style reference image). Only charged when the API actually
# returns a result (status 'done' or 'flagged_color_mismatch'); a hard
# failure before any image is produced is logged at ₹0/no cost.
COST_PER_GENERATION_USD = 0.039 + 0.0002

_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")


class PhotoEnhancementError(ValueError):
    """Raised for seller-facing validation failures (bad input, not found, etc.)."""


# ── Image I/O ──────────────────────────────────────────────────────────────────

async def _load_image_bytes(url_or_path: str) -> bytes:
    """
    Load image bytes from either a remote URL or a local /uploads/ path.

    Args:
        url_or_path: Full "http(s)://..." URL, or a local "/uploads/..." path
                     (possibly nested, e.g. "/uploads/style_references/x.png")
                     as returned by the upload endpoint or the style seed script.

    Returns:
        Raw image bytes.
    """
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url_or_path)
            resp.raise_for_status()
            return resp.content

    relative_path = url_or_path.removeprefix("/uploads/").removeprefix("uploads/")
    filepath = os.path.join(_UPLOADS_DIR, relative_path)
    with open(filepath, "rb") as f:
        return f.read()


def _save_generated_image(client_id: int, image_bytes: bytes) -> str:
    """
    Persist a generated image to the same uploads/ convention as seller uploads.

    Returns:
        Public "/uploads/{filename}" URL.
    """
    filename = f"{client_id}_{uuid.uuid4().hex[:12]}_enhanced.png"
    os.makedirs(_UPLOADS_DIR, exist_ok=True)
    with open(os.path.join(_UPLOADS_DIR, filename), "wb") as f:
        f.write(image_bytes)
    return f"/uploads/{filename}"


# ── Pre-check gate ─────────────────────────────────────────────────────────────

def check_image_quality(image_bytes: bytes) -> tuple[bool, Optional[str]]:
    """
    Validate a raw variant photo before spending a Gemini call on it.

    Checks:
      - minimum resolution: both dimensions >= MIN_RESOLUTION_PX
      - blur: variance of an edge-detected grayscale copy must clear
        BLUR_VARIANCE_THRESHOLD (a flat/blurry image has low edge variance)

    Args:
        image_bytes: Raw bytes of the seller's uploaded variant photo.

    Returns:
        (True, None) if the image passes, else (False, seller-facing reason).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception:
        return False, "Could not read this image file. Please upload a valid JPEG/PNG/WebP photo."

    width, height = img.size
    if width < MIN_RESOLUTION_PX or height < MIN_RESOLUTION_PX:
        return False, (
            f"Image resolution is {width}x{height}, below the {MIN_RESOLUTION_PX}x{MIN_RESOLUTION_PX} "
            "minimum needed for photo enhancement. Please upload a higher-resolution photo."
        )

    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    variance = ImageStat.Stat(edges).var[0]
    if variance < BLUR_VARIANCE_THRESHOLD:
        return False, "This photo looks blurry or low-detail. Please upload a sharper photo."

    return True, None


# ── Gemini fusion call ─────────────────────────────────────────────────────────

def build_prompt(style_reference: StyleReference) -> str:
    """
    Build the Gemini fusion prompt for a given style reference.

    Same image+image fusion path for every style_type (dummy/human_model/hanging)
    — validated in manual AI Studio testing that an AI-generated pose/model
    reference fuses reliably with the product garment image without tripping
    safety filters, so no special-casing is needed per style_type.
    """
    return (
        "Image 1 shows a pose, lighting, and setting reference. "
        "Image 2 shows a garment with its exact color, pattern, and border design.\n\n"
        "Generate a new photorealistic image: a subject in the same pose, styling, and setting "
        "as Image 1, now wearing/displaying the garment from Image 2. Preserve the garment's "
        "exact color, pattern, and design details with zero alteration — blend naturally as one "
        f"cohesive photograph. Style context: {style_reference.description}. "
        "Professional fashion photography style, sharp focus, natural lighting."
    )


def _get_genai_client() -> genai.Client:
    """Return a Gemini client using the platform's GEMINI_API_KEY."""
    return genai.Client(api_key=get_settings().gemini_api_key)


async def _call_gemini_fusion(raw_bytes: bytes, style_bytes: bytes, prompt: str) -> bytes:
    """
    Call Gemini 2.5 Flash Image with the style reference image + raw garment photo.

    Image order matters: the prompt refers to "Image 1" as the pose/lighting/
    setting reference and "Image 2" as the garment, so the style reference must
    be sent before the raw product photo.

    Args:
        raw_bytes:   Seller's raw uploaded variant photo (Image 2).
        style_bytes: The selected style_reference's reference image (Image 1).
        prompt:      Fusion instructions (see build_prompt).

    Returns:
        Generated image bytes.

    Raises:
        PhotoEnhancementError: If Gemini returns no image part.
    """
    client = _get_genai_client()
    response = await client.aio.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=[
            prompt,
            types.Part.from_bytes(data=style_bytes, mime_type="image/jpeg"),
            types.Part.from_bytes(data=raw_bytes, mime_type="image/jpeg"),
        ],
    )

    candidates = response.candidates or []
    for candidate in candidates:
        for part in candidate.content.parts or []:
            if getattr(part, "inline_data", None) is not None:
                return part.inline_data.data

    raise PhotoEnhancementError("Gemini did not return an image for this generation.")


# ── Color QC ───────────────────────────────────────────────────────────────────

def color_qc(raw_bytes: bytes, generated_bytes: bytes) -> tuple[bool, float]:
    """
    Compare dominant-color histograms of the raw upload vs the generated image.

    Args:
        raw_bytes:       Original seller-uploaded photo bytes.
        generated_bytes: Gemini-generated output bytes.

    Returns:
        (flagged, similarity) where flagged=True means the colour drifted too
        far to auto-publish, and similarity is the raw normalized histogram
        intersection score (1.0 = identical, 0.0 = completely different).
    """
    raw_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB").resize((128, 128))
    gen_img = Image.open(io.BytesIO(generated_bytes)).convert("RGB").resize((128, 128))

    raw_hist = raw_img.histogram()
    gen_hist = gen_img.histogram()

    intersection = sum(min(a, b) for a, b in zip(raw_hist, gen_hist))
    total = sum(raw_hist)
    similarity = intersection / total if total else 0.0

    flagged = similarity < COLOR_MISMATCH_THRESHOLD
    return flagged, similarity


# ── Orchestration ──────────────────────────────────────────────────────────────

async def _get_owned_variant(
    db: AsyncSession, client_id: int, product_id: int, variant_id: int
) -> ProductVariant:
    """Fetch a variant, ensuring both it and its parent product belong to client_id."""
    result = await db.execute(
        select(ProductVariant).where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
            ProductVariant.client_id == client_id,
        )
    )
    variant = result.scalar_one_or_none()
    if variant is None:
        raise PhotoEnhancementError("Variant not found.")
    return variant


async def list_style_references(
    db: AsyncSession, category: str, style_type: Optional[str] = None
) -> list[StyleReference]:
    """
    Return active style references for a product category, for the dashboard's
    style picker.

    Includes both category-specific rows and 'universal' rows (selectable
    regardless of product category) — a style reference doesn't need to be
    duplicated per category just because it applies to all of them.

    Args:
        db:         Active async DB session.
        category:   Product category, e.g. "Saree" (case-insensitive).
        style_type: Optional filter — one of dummy/human_model/hanging.

    Returns:
        List of active StyleReference rows.
    """
    stmt = select(StyleReference).where(
        StyleReference.active == True,  # noqa: E712
        or_(
            StyleReference.category.ilike(category),
            StyleReference.category.ilike("universal"),
        ),
    )
    if style_type:
        stmt = stmt.where(StyleReference.style_type == style_type)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def generate_variant_photo(
    db: AsyncSession,
    client_id: int,
    product_id: int,
    variant_id: int,
    style_reference_id: int,
) -> ProductVariant:
    """
    Run the full enhance-photo flow for one (variant, style) pair.

    Pre-checks the raw upload, calls Gemini (with one retry on failure), runs
    color QC on success, and persists the result — enhanced_status ends as
    'done', 'flagged_color_mismatch', or 'failed'. Always logs one
    photo_generation_log row.

    Args:
        db:                  Active async DB session.
        client_id:           Owning client (tenant scope).
        product_id:          Parent product PK.
        variant_id:          Variant PK to enhance.
        style_reference_id:  Selected StyleReference PK.

    Returns:
        The updated ProductVariant.

    Raises:
        PhotoEnhancementError: Not found, wrong tenant, or the raw photo fails
            the pre-check quality gate. Never raised for Gemini call failures —
            those are captured as enhanced_status='failed' instead.
    """
    variant = await _get_owned_variant(db, client_id, product_id, variant_id)
    if not variant.image_url:
        raise PhotoEnhancementError("This variant has no uploaded photo to enhance.")

    style_result = await db.execute(
        select(StyleReference).where(
            StyleReference.id == style_reference_id,
            StyleReference.active == True,  # noqa: E712
        )
    )
    style = style_result.scalar_one_or_none()
    if style is None:
        raise PhotoEnhancementError("Style reference not found or inactive.")

    raw_bytes = await _load_image_bytes(variant.image_url)

    ok, reason = check_image_quality(raw_bytes)
    if not ok:
        raise PhotoEnhancementError(reason)

    variant.enhanced_status = "pending"
    variant.style_reference_id = style.id
    await db.commit()

    style_bytes = await _load_image_bytes(style.reference_image_url)
    prompt = build_prompt(style)

    generated_bytes: Optional[bytes] = None
    last_error: Optional[Exception] = None
    for attempt in range(2):  # one retry on failure
        try:
            generated_bytes = await _call_gemini_fusion(raw_bytes, style_bytes, prompt)
            break
        except Exception as exc:  # noqa: BLE001 — any Gemini/network failure retries once
            last_error = exc
            logger.warning(
                "photo_enhancement: Gemini call failed (attempt %d) for variant %d: %s",
                attempt + 1, variant_id, exc,
            )

    if generated_bytes is None:
        variant.enhanced_status = "failed"
        await db.commit()
        await _log_generation(db, client_id, product_id, variant_id, style.id, "failed", cost_usd=0.0)
        logger.error("photo_enhancement: failed after retry for variant %d: %s", variant_id, last_error)
        return variant

    flagged, similarity = color_qc(raw_bytes, generated_bytes)
    variant.enhanced_image_url = _save_generated_image(client_id, generated_bytes)
    variant.enhanced_status = "flagged_color_mismatch" if flagged else "done"
    variant.enhanced_approved = False  # every new generation needs a fresh seller approval
    await db.commit()
    await db.refresh(variant)

    logger.info(
        "photo_enhancement: variant %d -> %s (color similarity %.2f)",
        variant_id, variant.enhanced_status, similarity,
    )
    await _log_generation(
        db, client_id, product_id, variant_id, style.id, variant.enhanced_status,
        cost_usd=COST_PER_GENERATION_USD,
    )
    return variant


async def _log_generation(
    db: AsyncSession,
    client_id: int,
    product_id: int,
    variant_id: int,
    style_reference_id: Optional[int],
    status: str,
    cost_usd: float,
) -> None:
    """Append one row to photo_generation_log for COGS tracking."""
    is_overage, overage_price_inr = await billing_service.check_image_quota_and_bill_overage(
        db, client_id
    )
    db.add(PhotoGenerationLog(
        client_id=client_id,
        product_id=product_id,
        variant_id=variant_id,
        style_reference_id=style_reference_id,
        status=status,
        cost_estimate_usd=cost_usd,
        is_overage=is_overage,
        overage_price_inr=overage_price_inr,
    ))
    await db.commit()


async def approve_variant_photo(
    db: AsyncSession, client_id: int, product_id: int, variant_id: int
) -> ProductVariant:
    """
    Mark a variant's latest enhanced photo as seller-approved for publishing.

    Args:
        db:         Active async DB session.
        client_id:  Owning client (tenant scope).
        product_id: Parent product PK.
        variant_id: Variant PK.

    Returns:
        The updated ProductVariant.

    Raises:
        PhotoEnhancementError: Not found, or enhanced_status is not 'done'
            (flagged/failed/pending photos cannot be approved).
    """
    variant = await _get_owned_variant(db, client_id, product_id, variant_id)
    if variant.enhanced_status != "done":
        raise PhotoEnhancementError(
            "Only a successfully generated, unflagged photo can be approved."
        )
    variant.enhanced_approved = True
    await db.commit()
    await db.refresh(variant)
    return variant


async def reject_variant_photo(
    db: AsyncSession, client_id: int, product_id: int, variant_id: int
) -> ProductVariant:
    """
    Discard a variant's latest enhanced photo so the seller can retry with a
    different style; the original raw image_url is untouched.

    Args:
        db:         Active async DB session.
        client_id:  Owning client (tenant scope).
        product_id: Parent product PK.
        variant_id: Variant PK.

    Returns:
        The updated ProductVariant.
    """
    variant = await _get_owned_variant(db, client_id, product_id, variant_id)
    variant.enhanced_image_url = None
    variant.enhanced_status = None
    variant.enhanced_approved = False
    await db.commit()
    await db.refresh(variant)
    return variant
