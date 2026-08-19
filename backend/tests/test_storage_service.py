"""Tests for app/services/storage_service.py."""

import asyncio
import io
import os
from unittest.mock import AsyncMock, MagicMock, patch

from botocore.exceptions import ClientError
from PIL import Image

from app.services import storage_service as svc


def _png_bytes(size=(2000, 1000), color=(120, 60, 200)) -> bytes:
    """Build in-memory PNG bytes for test images."""
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── compress_image ───────────────────────────────────────────────────────────

def test_compress_image_downscales_wide_image_to_max_width():
    """A wider-than-1200px image is downscaled to exactly 1200px wide, aspect kept."""
    out = svc.compress_image(_png_bytes(size=(2000, 1000)))
    img = Image.open(io.BytesIO(out))
    assert img.width == svc.MAX_IMAGE_WIDTH
    assert img.height == 600  # 1000 * (1200/2000)
    assert img.format == "JPEG"


def test_compress_image_does_not_upscale_narrow_image():
    """An image already narrower than the max width is left at its own width."""
    out = svc.compress_image(_png_bytes(size=(400, 300)))
    img = Image.open(io.BytesIO(out))
    assert img.width == 400
    assert img.height == 300


# ── is_r2_configured ─────────────────────────────────────────────────────────

def test_is_r2_configured_false_when_settings_missing(monkeypatch):
    """Missing any required R2 setting means R2 uploads are disabled."""
    settings = MagicMock(
        r2_endpoint="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name=""
    )
    with patch("app.services.storage_service.get_settings", return_value=settings):
        assert svc.is_r2_configured() is False


def test_is_r2_configured_true_when_all_settings_present():
    """All four required R2 settings present means R2 uploads are enabled."""
    settings = MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="bucket",
    )
    with patch("app.services.storage_service.get_settings", return_value=settings):
        assert svc.is_r2_configured() is True


# ── upload_product_image ─────────────────────────────────────────────────────

def test_upload_product_image_uses_r2_when_configured():
    """When R2 is configured, the image is put to the bucket and a public URL built from R2_PUBLIC_BASE_URL is returned."""
    settings = MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="my-bucket",
        r2_public_base_url="https://media.example.com",
    )
    mock_client = MagicMock()
    svc._r2_client.cache_clear()
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client):
        url = svc.upload_product_image(client_id=7, contents=_png_bytes(), product_id=42)

    assert url.startswith("https://media.example.com/7/42/")
    assert url.endswith(".jpg")
    mock_client.put_object.assert_called_once()
    kwargs = mock_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["Key"].startswith("7/42/")
    assert kwargs["ContentType"] == "image/jpeg"
    svc._r2_client.cache_clear()


def test_upload_product_image_unassigned_prefix_when_no_product_id():
    """A new product (no ID yet) uploads under the 'unassigned' prefix."""
    settings = MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="my-bucket",
        r2_public_base_url="https://media.example.com",
    )
    mock_client = MagicMock()
    svc._r2_client.cache_clear()
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client):
        url = svc.upload_product_image(client_id=7, contents=_png_bytes(), product_id=None)

    assert url.startswith("https://media.example.com/7/unassigned/")
    svc._r2_client.cache_clear()


def test_upload_product_image_falls_back_to_local_disk_when_r2_not_configured(tmp_path):
    """With no R2 credentials set, the image is written to the local uploads/ dir instead."""
    settings = MagicMock(
        r2_endpoint="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name="",
        r2_public_base_url="",
    )
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service._UPLOADS_DIR", str(tmp_path)):
        url = svc.upload_product_image(client_id=7, contents=_png_bytes(), product_id=None)

    assert url.startswith("/uploads/7_")
    saved_path = os.path.join(str(tmp_path), url.removeprefix("/uploads/"))
    assert os.path.isfile(saved_path)


# ── watermark_sku ─────────────────────────────────────────────────────────────

def test_watermark_sku_returns_a_different_valid_jpeg():
    """Watermarking returns a different, still-valid JPEG at the same dimensions."""
    original = _png_bytes(size=(800, 600))
    out = svc.watermark_sku(original, "pr10983")
    assert out != svc.compress_image(original)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (800, 600)


def test_watermark_sku_does_not_mutate_input_bytes():
    """The caller's original bytes object is left untouched (a copy is returned)."""
    original = _png_bytes()
    original_copy = bytes(original)
    svc.watermark_sku(original, "PR10983")
    assert original == original_copy


# ── get_watermarked_image_url ───────────────────────────────────────────────

def _r2_settings():
    return MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="my-bucket",
        r2_public_base_url="https://media.example.com",
    )


def _mock_http_get(content: bytes):
    """Patch target for storage_service.httpx.AsyncClient returning `content`."""
    mock_resp = MagicMock()
    mock_resp.content = content
    mock_resp.raise_for_status = MagicMock()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=mock_resp)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_http)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_ctx)


def test_get_watermarked_image_url_generates_and_uploads_on_cache_miss():
    """No cached object yet: fetch the source, watermark it, and upload to the deterministic key."""
    settings = _r2_settings()
    mock_client = MagicMock()
    mock_client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    svc._r2_client.cache_clear()

    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client), \
         patch("app.services.storage_service.httpx.AsyncClient", _mock_http_get(_png_bytes())):
        url = asyncio.run(
            svc.get_watermarked_image_url(7, "PR10983", "https://cdn.example.com/x.jpg")
        )

    assert url == "https://media.example.com/7/watermarked/PR10983.jpg"
    mock_client.put_object.assert_called_once()
    kwargs = mock_client.put_object.call_args.kwargs
    assert kwargs["Key"] == "7/watermarked/PR10983.jpg"
    svc._r2_client.cache_clear()


def test_get_watermarked_image_url_reuses_cached_object():
    """An already-generated watermarked object skips fetch/watermark/upload entirely."""
    settings = _r2_settings()
    mock_client = MagicMock()
    mock_client.head_object.return_value = {}
    svc._r2_client.cache_clear()
    mock_http_cls = _mock_http_get(_png_bytes())

    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client), \
         patch("app.services.storage_service.httpx.AsyncClient", mock_http_cls):
        url = asyncio.run(
            svc.get_watermarked_image_url(7, "PR10983", "https://cdn.example.com/x.jpg")
        )

    assert url == "https://media.example.com/7/watermarked/PR10983.jpg"
    mock_client.put_object.assert_not_called()
    mock_http_cls.assert_not_called()
    svc._r2_client.cache_clear()


def test_get_watermarked_image_url_returns_none_when_source_fetch_fails():
    """A failed source-image fetch must not raise — caller falls back to the raw URL."""
    settings = MagicMock(
        r2_endpoint="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name="",
        r2_public_base_url="",
    )
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("network error"))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.httpx.AsyncClient", return_value=mock_ctx):
        url = asyncio.run(
            svc.get_watermarked_image_url(7, "PR10983", "https://cdn.example.com/x.jpg")
        )

    assert url is None


def test_get_watermarked_image_url_local_disk_fallback(tmp_path):
    """With no R2 credentials set, the watermarked copy is written to uploads/ instead."""
    settings = MagicMock(
        r2_endpoint="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name="",
        r2_public_base_url="",
    )
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service._UPLOADS_DIR", str(tmp_path)), \
         patch("app.services.storage_service.httpx.AsyncClient", _mock_http_get(_png_bytes())):
        url = asyncio.run(
            svc.get_watermarked_image_url(7, "PR10983", "https://cdn.example.com/x.jpg")
        )

    assert url.startswith("/uploads/wm_7_")
    saved_path = os.path.join(str(tmp_path), url.removeprefix("/uploads/"))
    assert os.path.isfile(saved_path)


# ── upload_file ───────────────────────────────────────────────────────────────

def test_upload_file_uploads_to_r2_when_configured():
    """upload_file puts the given bytes under the given key and returns a public URL."""
    settings = MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="my-bucket",
        r2_public_base_url="https://media.example.com",
    )
    mock_client = MagicMock()
    svc._r2_client.cache_clear()
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client):
        url = svc.upload_file("invoices/order_invoice_42.pdf", b"%PDF-fake", "application/pdf")

    assert url == "https://media.example.com/invoices/order_invoice_42.pdf"
    mock_client.put_object.assert_called_once_with(
        Bucket="my-bucket", Key="invoices/order_invoice_42.pdf",
        Body=b"%PDF-fake", ContentType="application/pdf",
    )
    svc._r2_client.cache_clear()


def test_upload_file_returns_none_when_r2_not_configured():
    """Without R2 credentials, upload_file returns None so callers fall back to local disk."""
    settings = MagicMock(
        r2_endpoint="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name="",
    )
    with patch("app.services.storage_service.get_settings", return_value=settings):
        url = svc.upload_file("invoices/order_invoice_42.pdf", b"%PDF-fake", "application/pdf")

    assert url is None


def test_upload_file_returns_none_when_public_base_url_unset():
    """R2 configured but no public base URL — object is uploaded but the URL is unusable, so return None."""
    settings = MagicMock(
        r2_endpoint="https://x.r2.cloudflarestorage.com",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket_name="my-bucket",
        r2_public_base_url="",
    )
    mock_client = MagicMock()
    svc._r2_client.cache_clear()
    with patch("app.services.storage_service.get_settings", return_value=settings), \
         patch("app.services.storage_service.boto3.client", return_value=mock_client):
        url = svc.upload_file("invoices/order_invoice_42.pdf", b"%PDF-fake", "application/pdf")

    assert url is None
    mock_client.put_object.assert_called_once()
    svc._r2_client.cache_clear()
