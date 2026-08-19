"""
Tesseract OCR — reads the SKU watermark storage_service.watermark_sku() burns
into outgoing product photos, so a customer who screenshots a product photo
and sends it back later (often with no text at all) can still be matched to
the right product.

Local and free (~₹0/image), chosen over Groq's vision LLM (vision_service.py)
for this specific case: this only ever needs to read a small, fixed-position
"SKU: XXXXX" text watermark, not general product recognition, so a full
vision-LLM call is unnecessary cost — and unlike the LLM path, OCR has no
external API to 404 or drift model versions on.

Deployment note: pytesseract only binds to the `tesseract` binary — it does
NOT install it. The binary itself must be present on the host for this to
work at all (see requirements.txt).
"""

from __future__ import annotations

import io
import logging

import pytesseract
from PIL import Image

from app.services.catalogue_service import extract_skus_from_text
from app.services.storage_service import WATERMARK_CROP_FRACTION_H, WATERMARK_CROP_FRACTION_W

logger = logging.getLogger(__name__)


def check_tesseract_installed() -> bool:
    """
    Verify the Tesseract binary is present on PATH, called once at app startup.

    pytesseract is only a Python wrapper — it does not bundle the OCR engine,
    so a missing binary otherwise surfaces as a silent None from every
    detect_sku_from_image() call instead of a clear boot-time error. Same
    pattern as the WhatsApp/Instagram token checks in main.py: log CRITICAL
    with install instructions rather than failing silently.

    Returns:
        True if Tesseract is available, False otherwise.
    """
    try:
        version = pytesseract.get_tesseract_version()
        logger.info("Tesseract OCR: VALID ✓ | version=%s", version)
        return True
    except Exception as exc:
        logger.critical(
            "Tesseract binary NOT FOUND on PATH — SKU OCR matching will fail on "
            "every call. Install it: macOS -> `brew install tesseract`, "
            "Railway/Docker -> add `tesseract-ocr` to the apt-get install step "
            "in backend/Dockerfile. Error: %s",
            exc,
        )
        return False


def detect_sku_from_image(image_bytes: bytes) -> str | None:
    """
    Crop the bottom-right watermark region and OCR it for a SKU-shaped token.

    Crops to the same corner storage_service.watermark_sku() draws into
    (WATERMARK_CROP_FRACTION_W/H, kept in sync with it) before running
    Tesseract, both for speed (much smaller image than the full photo) and
    accuracy (skips OCR noise from the rest of the product photo).

    Never raises — a corrupt image, a missing Tesseract binary, or an OCR
    failure all just return None so callers can fall back gracefully
    instead of erroring the whole message.

    Args:
        image_bytes: Raw bytes of the incoming customer image.

    Returns:
        The first SKU-shaped token found (uppercased), or None.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        crop_left = int(width * (1 - WATERMARK_CROP_FRACTION_W))
        crop_top = int(height * (1 - WATERMARK_CROP_FRACTION_H))
        crop = image.crop((crop_left, crop_top, width, height))

        text = pytesseract.image_to_string(crop)
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return None

    skus = extract_skus_from_text(text)
    return skus[0] if skus else None
