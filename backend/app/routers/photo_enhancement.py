"""
Product photo enhancement router.

Lets a seller turn a raw flat-lay variant photo into a styled dummy/
human-model/hanging shot via Gemini, then approve/reject the result before it
is ever shown on the storefront or in WA/IG.

All endpoints are JWT-protected and scoped to the authenticated client's own
products/variants — mirrors app/routers/catalogue.py's ownership checks.

Endpoints:
- GET  /catalogue/style-references                                — style picker options
- POST /catalogue/products/{id}/variants/{id}/enhance-photo       — run Gemini generation
- POST /catalogue/products/{id}/variants/{id}/approve-photo       — publish the enhanced photo
- POST /catalogue/products/{id}/variants/{id}/reject-photo        — discard, allow retry
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client, require_permission
from app.services import photo_enhancement_service as svc

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/catalogue",
    tags=["photo-enhancement"],
    dependencies=[Depends(require_permission("catalog_edit"))],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class StyleReferenceOut(BaseModel):
    """One style picker option."""

    id: int
    category: str
    style_type: str
    reference_image_url: str
    description: str

    model_config = {"from_attributes": True}


class EnhancePhotoRequest(BaseModel):
    """Request body for POST .../enhance-photo."""

    style_reference_id: int


class VariantPhotoOut(BaseModel):
    """Variant photo state returned by enhance/approve/reject."""

    id: int
    image_url: Optional[str] = None
    enhanced_image_url: Optional[str] = None
    enhanced_status: Optional[str] = None
    enhanced_approved: bool = False
    style_reference_id: Optional[int] = None
    display_image_url: Optional[str] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_variant(cls, v) -> "VariantPhotoOut":
        return cls(
            id=v.id,
            image_url=v.image_url,
            enhanced_image_url=v.enhanced_image_url,
            enhanced_status=v.enhanced_status,
            enhanced_approved=v.enhanced_approved,
            style_reference_id=v.style_reference_id,
            display_image_url=v.display_image_url,
        )


# ── Style reference picker ─────────────────────────────────────────────────────

@router.get("/style-references", response_model=list[StyleReferenceOut])
async def get_style_references(
    _: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
    category: str = Query(..., description="Product category, e.g. 'Saree'"),
    style_type: Optional[str] = Query(default=None, description="dummy | human_model | hanging"),
) -> list[StyleReferenceOut]:
    """
    Return active style references for the dashboard's style picker.

    Includes both rows matching `category` and rows with category='universal'
    (selectable regardless of product category).

    Args:
        category:   Product category to filter by (case-insensitive).
        style_type: Optional narrower filter.

    Returns:
        List of StyleReferenceOut, possibly empty.
    """
    styles = await svc.list_style_references(db, category, style_type)
    return [StyleReferenceOut.model_validate(s) for s in styles]


# ── Enhance / approve / reject ─────────────────────────────────────────────────

@router.post(
    "/products/{product_id}/variants/{variant_id}/enhance-photo",
    response_model=VariantPhotoOut,
)
async def enhance_photo(
    product_id: int,
    variant_id: int,
    body: EnhancePhotoRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> VariantPhotoOut:
    """
    Generate a styled photo for one variant using the selected style reference.

    Args:
        product_id:     Parent product PK (must belong to current_client).
        variant_id:     Variant PK to enhance (must belong to product_id).
        body:           style_reference_id to apply.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        VariantPhotoOut with the resulting enhanced_status ('done',
        'flagged_color_mismatch', or 'failed').

    Raises:
        HTTPException 400: Variant/style not found, no raw photo, or the raw
            photo fails the quality gate (low-res/blurry).
    """
    try:
        variant = await svc.generate_variant_photo(
            db, current_client.id, product_id, variant_id, body.style_reference_id
        )
    except svc.PhotoEnhancementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return VariantPhotoOut.from_variant(variant)


@router.post(
    "/products/{product_id}/variants/{variant_id}/approve-photo",
    response_model=VariantPhotoOut,
)
async def approve_photo(
    product_id: int,
    variant_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> VariantPhotoOut:
    """
    Approve the variant's latest enhanced photo for publishing.

    Args:
        product_id:     Parent product PK (must belong to current_client).
        variant_id:     Variant PK (must belong to product_id).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Updated VariantPhotoOut.

    Raises:
        HTTPException 400: Variant not found, or its enhanced_status isn't 'done'.
    """
    try:
        variant = await svc.approve_variant_photo(db, current_client.id, product_id, variant_id)
    except svc.PhotoEnhancementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return VariantPhotoOut.from_variant(variant)


@router.post(
    "/products/{product_id}/variants/{variant_id}/reject-photo",
    response_model=VariantPhotoOut,
)
async def reject_photo(
    product_id: int,
    variant_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> VariantPhotoOut:
    """
    Discard the variant's latest enhanced photo so the seller can retry.

    Args:
        product_id:     Parent product PK (must belong to current_client).
        variant_id:     Variant PK (must belong to product_id).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Updated VariantPhotoOut (enhanced fields cleared, raw image_url untouched).

    Raises:
        HTTPException 400: Variant not found.
    """
    try:
        variant = await svc.reject_variant_photo(db, current_client.id, product_id, variant_id)
    except svc.PhotoEnhancementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return VariantPhotoOut.from_variant(variant)
