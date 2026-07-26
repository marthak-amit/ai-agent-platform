"""Tests for app/services/photo_enhancement_service.py."""

import io
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.models.client import Client
from app.models.product_variant import ProductVariant
from app.models.style_reference import StyleReference
from app.services import photo_enhancement_service as svc


def _png_bytes(size=(900, 900), color=(120, 60, 200), noisy=False) -> bytes:
    """Build in-memory PNG bytes for test images."""
    img = Image.new("RGB", size, color)
    if noisy:
        # Checkerboard pattern gives high edge variance so the blur gate passes.
        pixels = img.load()
        for x in range(0, size[0], 2):
            for y in range(0, size[1], 2):
                pixels[x, y] = (255 - color[0], 255 - color[1], 255 - color[2])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def db():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


# ── check_image_quality ────────────────────────────────────────────────────────

def test_check_image_quality_passes_sharp_high_res_image():
    """A sharp, high-resolution image clears both checks."""
    ok, reason = svc.check_image_quality(_png_bytes(size=(1000, 1000), noisy=True))
    assert ok is True
    assert reason is None


def test_check_image_quality_rejects_low_resolution():
    """An image below MIN_RESOLUTION_PX fails with a seller-facing reason."""
    ok, reason = svc.check_image_quality(_png_bytes(size=(400, 400), noisy=True))
    assert ok is False
    assert "resolution" in reason.lower()


def test_check_image_quality_rejects_blurry_flat_image():
    """A flat, edge-less image fails the blur heuristic."""
    ok, reason = svc.check_image_quality(_png_bytes(size=(900, 900), noisy=False))
    assert ok is False
    assert "blurry" in reason.lower()


# ── color_qc ────────────────────────────────────────────────────────────────────

def test_color_qc_not_flagged_for_identical_images():
    """Identical images score a perfect histogram match and are not flagged."""
    raw = _png_bytes(color=(200, 50, 50))
    flagged, similarity = svc.color_qc(raw, raw)
    assert flagged is False
    assert similarity == pytest.approx(1.0)


def test_color_qc_flagged_for_wildly_different_colors():
    """A generated image with an unrelated dominant colour is flagged."""
    raw = _png_bytes(color=(255, 0, 0))
    generated = _png_bytes(color=(0, 255, 255))
    flagged, similarity = svc.color_qc(raw, generated)
    assert flagged is True
    assert similarity < svc.COLOR_MISMATCH_THRESHOLD


# ── build_prompt ────────────────────────────────────────────────────────────────

def test_build_prompt_does_not_branch_on_style_type():
    """
    build_prompt() uses one shared image+image fusion template for every
    style_type — human_model no longer gets special-cased now that manual
    AI Studio testing confirmed it fuses reliably like dummy/hanging.
    """
    prompts = {
        style_type: svc.build_prompt(StyleReference(
            id=1, category="universal", style_type=style_type,
            reference_image_url="https://example.com/ref.png",
            description="same description", active=True,
        ))
        for style_type in ("dummy", "human_model", "hanging")
    }
    assert len(set(prompts.values())) == 1

    prompt = next(iter(prompts.values()))
    assert "Image 1" in prompt
    assert "Image 2" in prompt
    assert "same description" in prompt


# ── generate_variant_photo ───────────────────────────────────────────────────────

def _variant(client_id=1, product_id=10, variant_id=100, image_url="/uploads/raw.jpg"):
    return ProductVariant(
        id=variant_id, product_id=product_id, client_id=client_id,
        image_url=image_url, stock=5, is_active=True,
    )


def _style(style_id=5):
    return StyleReference(
        id=style_id, category="Saree", style_type="dummy",
        reference_image_url="/uploads/style_references/x.png",
        description="test style", active=True,
    )


def _billing_client(client_id=1, image_quota=20, overage_price=8):
    """
    A Client with a current (non-elapsed) billing cycle, so
    billing_service.check_image_quota_and_bill_overage's internal
    ensure_current_cycle() no-ops without needing plan_cache seeded.
    """
    return Client(
        id=client_id, email="x@y.com", hashed_password="h",
        plan_slug="starter", plan_image_quota_snapshot=image_quota,
        plan_image_overage_price_snapshot=overage_price,
        billing_cycle_start=date.today(), plan_grandfathered=False,
    )


def _quota_check_results(used_this_month=0, **billing_client_kwargs):
    """Two extra db.execute results consumed by _log_generation's overage check."""
    client_result = MagicMock()
    client_result.scalar_one_or_none.return_value = _billing_client(**billing_client_kwargs)
    count_result = MagicMock()
    count_result.scalar_one.return_value = used_this_month
    return [client_result, count_result]


async def test_generate_variant_photo_success_marks_done(db):
    """A successful Gemini call with matching colours ends enhanced_status='done'."""
    variant = _variant()
    style = _style()
    variant_result = MagicMock(); variant_result.scalar_one_or_none.return_value = variant
    style_result = MagicMock(); style_result.scalar_one_or_none.return_value = style
    db.execute.side_effect = [variant_result, style_result, *_quota_check_results()]

    raw = _png_bytes(size=(1000, 1000), noisy=True, color=(10, 200, 10))
    generated = _png_bytes(size=(1000, 1000), noisy=True, color=(10, 200, 10))

    with patch.object(svc, "_load_image_bytes", AsyncMock(side_effect=[raw, style_bytes_placeholder := _png_bytes()])), \
         patch.object(svc, "_call_gemini_fusion", AsyncMock(return_value=generated)), \
         patch.object(svc, "_save_generated_image", return_value="/uploads/1_abc_enhanced.png"):
        result = await svc.generate_variant_photo(db, 1, 10, 100, 5)

    assert result.enhanced_status == "done"
    assert result.enhanced_image_url == "/uploads/1_abc_enhanced.png"
    assert result.enhanced_approved is False


async def test_generate_variant_photo_flags_color_mismatch(db):
    """A successful Gemini call whose output colour drifted ends 'flagged_color_mismatch'."""
    variant = _variant()
    style = _style()
    variant_result = MagicMock(); variant_result.scalar_one_or_none.return_value = variant
    style_result = MagicMock(); style_result.scalar_one_or_none.return_value = style
    db.execute.side_effect = [variant_result, style_result, *_quota_check_results()]

    raw = _png_bytes(size=(1000, 1000), noisy=True, color=(255, 0, 0))
    generated = _png_bytes(size=(1000, 1000), noisy=True, color=(0, 255, 255))

    with patch.object(svc, "_load_image_bytes", AsyncMock(side_effect=[raw, _png_bytes()])), \
         patch.object(svc, "_call_gemini_fusion", AsyncMock(return_value=generated)), \
         patch.object(svc, "_save_generated_image", return_value="/uploads/1_abc_enhanced.png"):
        result = await svc.generate_variant_photo(db, 1, 10, 100, 5)

    assert result.enhanced_status == "flagged_color_mismatch"


async def test_generate_variant_photo_retries_once_then_fails(db):
    """Two consecutive Gemini failures mark the variant 'failed' without raising."""
    variant = _variant()
    style = _style()
    variant_result = MagicMock(); variant_result.scalar_one_or_none.return_value = variant
    style_result = MagicMock(); style_result.scalar_one_or_none.return_value = style
    db.execute.side_effect = [variant_result, style_result, *_quota_check_results()]

    raw = _png_bytes(size=(1000, 1000), noisy=True)

    with patch.object(svc, "_load_image_bytes", AsyncMock(side_effect=[raw, _png_bytes()])), \
         patch.object(svc, "_call_gemini_fusion", AsyncMock(side_effect=RuntimeError("boom"))) as gemini_mock:
        result = await svc.generate_variant_photo(db, 1, 10, 100, 5)

    assert result.enhanced_status == "failed"
    assert gemini_mock.await_count == 2  # initial attempt + one retry


async def test_generate_variant_photo_rejects_low_quality_raw_image(db):
    """The pre-check gate raises before any Gemini call is attempted."""
    variant = _variant()
    style = _style()
    variant_result = MagicMock(); variant_result.scalar_one_or_none.return_value = variant
    style_result = MagicMock(); style_result.scalar_one_or_none.return_value = style
    db.execute.side_effect = [variant_result, style_result]

    small_raw = _png_bytes(size=(300, 300))

    with patch.object(svc, "_load_image_bytes", AsyncMock(return_value=small_raw)):
        with pytest.raises(svc.PhotoEnhancementError):
            await svc.generate_variant_photo(db, 1, 10, 100, 5)


async def test_log_generation_tags_overage_row(db):
    """_log_generation tags is_overage/overage_price_inr from billing_service's check."""
    with patch.object(
        svc.billing_service, "check_image_quota_and_bill_overage",
        AsyncMock(return_value=(True, 6)),
    ):
        await svc._log_generation(db, 1, 10, 100, 5, "done", cost_usd=0.04)

    logged = db.add.call_args[0][0]
    assert logged.is_overage is True
    assert logged.overage_price_inr == 6


async def test_log_generation_no_overage_under_quota(db):
    """_log_generation leaves is_overage False when under the plan's image_quota."""
    with patch.object(
        svc.billing_service, "check_image_quota_and_bill_overage",
        AsyncMock(return_value=(False, None)),
    ):
        await svc._log_generation(db, 1, 10, 100, 5, "done", cost_usd=0.04)

    logged = db.add.call_args[0][0]
    assert logged.is_overage is False
    assert logged.overage_price_inr is None


async def test_generate_variant_photo_variant_not_found_raises(db):
    """Fetching a variant outside the caller's tenant raises PhotoEnhancementError."""
    empty_result = MagicMock(); empty_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [empty_result]

    with pytest.raises(svc.PhotoEnhancementError):
        await svc.generate_variant_photo(db, 1, 10, 999, 5)


# ── approve / reject ──────────────────────────────────────────────────────────

async def test_approve_variant_photo_requires_done_status(db):
    """Approving a flagged (not yet 'done') photo is rejected."""
    variant = _variant()
    variant.enhanced_status = "flagged_color_mismatch"
    result = MagicMock(); result.scalar_one_or_none.return_value = variant
    db.execute.return_value = result

    with pytest.raises(svc.PhotoEnhancementError):
        await svc.approve_variant_photo(db, 1, 10, 100)


async def test_approve_variant_photo_sets_approved_true(db):
    """Approving a 'done' photo flips enhanced_approved to True."""
    variant = _variant()
    variant.enhanced_status = "done"
    result = MagicMock(); result.scalar_one_or_none.return_value = variant
    db.execute.return_value = result

    updated = await svc.approve_variant_photo(db, 1, 10, 100)
    assert updated.enhanced_approved is True


async def test_reject_variant_photo_clears_enhanced_fields(db):
    """Rejecting clears enhanced_image_url/status/approved but keeps the raw image_url."""
    variant = _variant()
    variant.enhanced_status = "done"
    variant.enhanced_image_url = "/uploads/1_abc_enhanced.png"
    variant.enhanced_approved = True
    result = MagicMock(); result.scalar_one_or_none.return_value = variant
    db.execute.return_value = result

    updated = await svc.reject_variant_photo(db, 1, 10, 100)
    assert updated.enhanced_image_url is None
    assert updated.enhanced_status is None
    assert updated.enhanced_approved is False
    assert updated.image_url == "/uploads/raw.jpg"
