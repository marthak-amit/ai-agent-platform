"""
Product catalogue router.

All endpoints are JWT-protected and scoped to the authenticated client's
products only — clients can never see or modify each other's catalogues.

Endpoints:
- POST   /catalogue/products/upload-image  — upload product image
- POST   /catalogue/products               — create product (JSON)
- GET    /catalogue/products               — list with optional filters
- PUT    /catalogue/products/{id}          — partial update
- DELETE /catalogue/products/{id}          — delete
- POST   /catalogue/products/{id}/adjust-stock — stock adjustment + log
- GET    /catalogue/products/{id}/stock-history — last 10 log entries
"""

import logging
import os
import uuid
from typing import Annotated, Optional, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import catalogue_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/catalogue", tags=["catalogue"])

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


# ── Schemas ───────────────────────────────────────────────────────────────────

class VariantIn(BaseModel):
    """One variant row for create/update requests."""

    id: Optional[int] = None
    color: Optional[str] = None
    size: Optional[str] = None
    stock: int = 0
    price: Optional[float] = None


class ProductCreate(BaseModel):
    """Request body for POST /catalogue/products."""

    name: str
    price: float
    stock: Optional[int] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    category: Optional[str] = None
    is_active: bool = True
    low_stock_alert: int = 5
    has_variants: bool = False
    variants: list[VariantIn] = []


class ProductUpdate(BaseModel):
    """Request body for PUT /catalogue/products/{id}. All fields optional."""

    name: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    category: Optional[str] = None
    is_active: Optional[bool] = None
    low_stock_alert: Optional[int] = None
    has_variants: Optional[bool] = None
    variants: Optional[list[VariantIn]] = None


class VariantAdjustment(BaseModel):
    """A single per-variant stock override."""

    variant_id: int
    new_stock: int


class StockAdjustRequest(BaseModel):
    """Request body for POST /catalogue/products/{id}/adjust-stock."""

    adjustment: Optional[int] = None
    reason: str = "correction"
    adjustments: Optional[list[VariantAdjustment]] = None


class ProductVariantOut(BaseModel):
    """Variant representation nested inside ProductOut."""

    id: int
    color: Optional[str] = None
    size: Optional[str] = None
    material: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[float] = None
    stock: int
    is_active: bool = True
    image_url: Optional[str] = None

    model_config = {"from_attributes": True}


class ProductOut(BaseModel):
    """Product representation returned by all catalogue endpoints."""

    id: int
    client_id: int
    name: str
    price: float
    stock: Optional[int]
    description: Optional[str]
    image_url: Optional[str]
    sku: Optional[str] = None
    category: Optional[str] = None
    is_active: bool = True
    low_stock_alert: int = 5
    has_variants: bool = False
    variants: list[ProductVariantOut] = []
    available_colors: list[str] = []
    available_sizes: list[str] = []

    model_config = {"from_attributes": True}

    @classmethod
    def from_product(cls, p) -> "ProductOut":
        """Build a ProductOut, deriving available_colors/available_sizes from variants."""
        variants = [v for v in (p.variants or []) if v.is_active] if p.has_variants else []
        return cls(
            id=p.id,
            client_id=p.client_id,
            name=p.name,
            price=p.price,
            stock=p.stock,
            description=p.description,
            image_url=p.image_url,
            sku=p.sku,
            category=p.category,
            is_active=p.is_active,
            low_stock_alert=p.low_stock_alert,
            has_variants=p.has_variants,
            variants=[ProductVariantOut.model_validate(v) for v in variants],
            available_colors=list(dict.fromkeys(v.color for v in variants if v.color)),
            available_sizes=list(dict.fromkeys(v.size for v in variants if v.size)),
        )


class StockLogOut(BaseModel):
    """Stock adjustment log entry."""

    id: int
    product_id: int
    adjustment: int
    reason: str
    stock_before: int
    stock_after: int
    created_at: str

    model_config = {"from_attributes": True}


# ── Image upload ──────────────────────────────────────────────────────────────

@router.post("/products/upload-image", status_code=status.HTTP_200_OK)
async def upload_product_image(
    file: UploadFile = File(...),
    current_client: Client = Depends(get_current_client),
) -> dict:
    """
    Upload a product image and return its public URL.

    Saves to backend/uploads/ served as /uploads/{filename} via StaticFiles.

    Args:
        file:           Uploaded image file (JPEG, PNG, WebP, GIF; max 5 MB).
        current_client: JWT-authenticated Client.

    Returns:
        {"url": "/uploads/{filename}"}

    Raises:
        HTTPException 400: If file type is not allowed or exceeds size limit.
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image type '{file.content_type}' not allowed. Use JPEG, PNG, WebP, or GIF.",
        )

    contents = await file.read()
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image exceeds 5 MB limit.",
        )

    ext = (file.filename or "image.jpg").rsplit(".", 1)[-1].lower()
    filename = f"{current_client.id}_{uuid.uuid4().hex[:12]}.{ext}"

    os.makedirs(UPLOADS_DIR, exist_ok=True)
    filepath = os.path.join(UPLOADS_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(contents)

    logger.info("Image uploaded: %s by client %d", filename, current_client.id)
    return {"url": f"/uploads/{filename}"}


# ── Product CRUD ──────────────────────────────────────────────────────────────

@router.post(
    "/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_product(
    body: ProductCreate,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    """
    Add a product to the authenticated client's catalogue.

    Args:
        body:           Product data including new fields (sku, category, is_active, low_stock_alert).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Created ProductOut.
    """
    # Validate SKU uniqueness when caller provides one explicitly.
    if body.sku:
        existing = await catalogue_service.find_product_by_sku(
            db, current_client.id, body.sku
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"SKU '{body.sku}' already exists in your catalogue.",
            )

    product = await catalogue_service.create_product(
        db,
        client_id=current_client.id,
        name=body.name,
        price=body.price,
        stock=body.stock,
        description=body.description,
        image_url=body.image_url,
        sku=body.sku,
        category=body.category,
        is_active=body.is_active,
        low_stock_alert=body.low_stock_alert,
        has_variants=body.has_variants,
    )

    if body.has_variants and body.variants:
        variant_dicts = [v.model_dump() for v in body.variants]
        await catalogue_service.create_product_variants(
            db, product.id, current_client.id, variant_dicts
        )
        product.stock = sum(v.stock for v in body.variants)
        await db.commit()
        await db.refresh(product)

    return ProductOut.from_product(product)


@router.get(
    "/products/search",
    response_model=Optional[ProductOut],
)
async def search_product_by_sku(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
    sku: str = Query(..., description="Exact SKU to look up, e.g. SR27754"),
) -> Optional[ProductOut]:
    """
    Find a product by exact SKU.

    Used by the AI agent to resolve product codes mentioned in customer
    messages (e.g. "SR27754 ka price kya hai?").

    Args:
        sku:            Exact SKU string, e.g. "SR27754" (case-sensitive).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        ProductOut if found, or null (HTTP 200 with null body) if not found.
    """
    product = await catalogue_service.find_product_by_sku(
        db, current_client.id, sku
    )
    return ProductOut.from_product(product) if product else None


@router.get(
    "/products",
    response_model=list[ProductOut],
)
async def list_products(
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
    category: Optional[str] = Query(default=None),
    low_stock: Optional[bool] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> list[ProductOut]:
    """
    Return the authenticated client's products with optional filtering.

    Query params:
        category:   Filter by category name (exact match, case-sensitive).
        low_stock:  If true, return only products at/below their low_stock_alert.
        is_active:  Filter by active status (true = active only, false = inactive only).
        search:     Full-text keyword filter on name, description, category, SKU.

    Args:
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        List of ProductOut matching all provided filters (may be empty).
    """
    products = await catalogue_service.list_products(
        db,
        current_client.id,
        category=category,
        low_stock_only=bool(low_stock),
        is_active=is_active,
        search=search,
    )
    return [ProductOut.from_product(p) for p in products]


@router.put(
    "/products/{product_id}",
    response_model=ProductOut,
)
async def update_product(
    product_id: int,
    body: ProductUpdate,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    """
    Update an existing product. Only provided fields are changed.

    Args:
        product_id:     Product PK (must belong to current_client).
        body:           Partial update payload with any product fields.
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Updated ProductOut.

    Raises:
        HTTPException 404: If product not found or owned by another client.
    """
    product = await catalogue_service.get_product(db, current_client.id, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    is_active_val: Any = catalogue_service._UNSET
    if body.is_active is not None:
        is_active_val = body.is_active

    has_variants_val: Any = catalogue_service._UNSET
    if body.has_variants is not None:
        has_variants_val = body.has_variants

    updated = await catalogue_service.update_product(
        db,
        product,
        name=body.name,
        price=body.price,
        stock=body.stock,
        description=body.description,
        image_url=body.image_url,
        sku=body.sku,
        category=body.category,
        is_active=is_active_val,
        low_stock_alert=body.low_stock_alert,
        has_variants=has_variants_val,
    )

    if body.variants is not None:
        await catalogue_service.sync_product_variants(
            db, updated, current_client.id,
            [v.model_dump() for v in body.variants]
        )
        updated.stock = sum(v.stock for v in body.variants)
        await db.commit()
        await db.refresh(updated)

    return ProductOut.from_product(updated)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Permanently delete a product.

    Args:
        product_id:     Product PK (must belong to current_client).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Raises:
        HTTPException 404: If product not found or owned by another client.
    """
    product = await catalogue_service.get_product(db, current_client.id, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    await catalogue_service.delete_product(db, product)


# ── Stock management ──────────────────────────────────────────────────────────

@router.post(
    "/products/{product_id}/adjust-stock",
    response_model=ProductOut,
)
async def adjust_stock(
    product_id: int,
    body: StockAdjustRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    """
    Apply a signed stock adjustment and record it in the stock_logs table.

    Args:
        product_id:     Product PK (must belong to current_client).
        body:           adjustment (±int) and reason (sold/restocked/correction/damaged).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        Updated ProductOut with new stock count.

    Raises:
        HTTPException 404: If product not found.
        HTTPException 400: If reason is invalid.
    """
    product = await catalogue_service.get_product(db, current_client.id, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    try:
        if body.adjustments:
            updated = await catalogue_service.adjust_variant_stocks(
                db, product, current_client.id,
                [a.model_dump() for a in body.adjustments],
                body.reason,
            )
        elif body.adjustment is not None:
            updated = await catalogue_service.adjust_stock(
                db, product, current_client.id, body.adjustment, body.reason
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide either 'adjustment' (int) or 'adjustments' (list).",
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return ProductOut.from_product(updated)


@router.get(
    "/products/{product_id}/stock-history",
    response_model=list[StockLogOut],
)
async def stock_history(
    product_id: int,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> list[StockLogOut]:
    """
    Return the last 10 stock adjustment events for a product.

    Args:
        product_id:     Product PK (must belong to current_client).
        current_client: JWT-authenticated Client.
        db:             Injected async DB session.

    Returns:
        List of StockLogOut, newest first.

    Raises:
        HTTPException 404: If product not found.
    """
    product = await catalogue_service.get_product(db, current_client.id, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    logs = await catalogue_service.get_stock_history(db, product_id)
    return [
        StockLogOut(
            id=log.id,
            product_id=log.product_id,
            adjustment=log.adjustment,
            reason=log.reason,
            stock_before=log.stock_before,
            stock_after=log.stock_after,
            created_at=log.created_at.isoformat(),
        )
        for log in logs
    ]
