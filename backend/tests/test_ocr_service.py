"""Tests for app/services/ocr_service.py."""

import io
from unittest.mock import patch

from PIL import Image

from app.services import ocr_service


def _image_bytes(size=(1000, 800), color=(50, 50, 50)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_detect_sku_from_image_returns_matched_sku():
    """A Tesseract result containing a SKU-shaped token is extracted and uppercased."""
    with patch(
        "app.services.ocr_service.pytesseract.image_to_string",
        return_value="SKU: pr10983\n",
    ):
        assert ocr_service.detect_sku_from_image(_image_bytes()) == "PR10983"


def test_detect_sku_from_image_returns_none_when_no_sku_shaped_token():
    """OCR text with no SKU-shaped token yields None, not an exception."""
    with patch(
        "app.services.ocr_service.pytesseract.image_to_string",
        return_value="blurry nonsense text",
    ):
        assert ocr_service.detect_sku_from_image(_image_bytes()) is None


def test_detect_sku_from_image_returns_none_on_ocr_exception():
    """A Tesseract/binary failure must not raise — callers get None and fall back gracefully."""
    with patch(
        "app.services.ocr_service.pytesseract.image_to_string",
        side_effect=RuntimeError("tesseract binary not found"),
    ):
        assert ocr_service.detect_sku_from_image(_image_bytes()) is None


def test_detect_sku_from_image_returns_none_on_corrupt_image():
    """Unreadable image bytes must not raise."""
    assert ocr_service.detect_sku_from_image(b"not an image") is None


def test_check_tesseract_installed_returns_true_when_binary_found():
    """A working Tesseract binary reports the version and returns True."""
    with patch(
        "app.services.ocr_service.pytesseract.get_tesseract_version",
        return_value="5.3.4",
    ):
        assert ocr_service.check_tesseract_installed() is True


def test_check_tesseract_installed_logs_critical_and_returns_false_when_missing(caplog):
    """A missing binary must log CRITICAL with install instructions, not raise."""
    with patch(
        "app.services.ocr_service.pytesseract.get_tesseract_version",
        side_effect=EnvironmentError("tesseract is not installed"),
    ):
        with caplog.at_level("CRITICAL"):
            result = ocr_service.check_tesseract_installed()

    assert result is False
    assert "brew install tesseract" in caplog.text
    assert "tesseract-ocr" in caplog.text


def test_detect_sku_from_image_crops_to_bottom_right_watermark_region():
    """OCR runs only on the bottom-right crop (Part 1's fixed watermark position), not the full photo."""
    captured = {}

    def _fake_ocr(crop_image):
        captured["size"] = crop_image.size
        return "SKU: SR27754"

    with patch("app.services.ocr_service.pytesseract.image_to_string", side_effect=_fake_ocr):
        sku = ocr_service.detect_sku_from_image(_image_bytes(size=(1000, 800)))

    assert sku == "SR27754"
    expected_w = 1000 - int(1000 * (1 - ocr_service.WATERMARK_CROP_FRACTION_W))
    expected_h = 800 - int(800 * (1 - ocr_service.WATERMARK_CROP_FRACTION_H))
    assert captured["size"] == (expected_w, expected_h)
