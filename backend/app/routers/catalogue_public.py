"""
Public catalogue router — no authentication required.

Endpoints:
- GET  /shop/{slug}                    : full catalogue (business info + products)
- GET  /shop/{slug}/product/{sku}      : single product detail
- GET  /shop/{slug}/search             : search products by q, category
- GET  /shop/{slug}/pdf                : download catalogue as PDF
- POST /shop/{slug}/notify-restock     : register restock notification
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.models.product import Product
from app.models.restock_notification import RestockNotification

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Public Catalogue"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_client_by_slug(slug: str, db: AsyncSession) -> Client:
    """Fetch client by catalogue_slug or raise 404."""
    result = await db.execute(select(Client).where(Client.catalogue_slug == slug))
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue not found.")
    return client


def _variant_dict(v) -> dict:
    """Serialise a ProductVariant ORM row to a plain dict for API responses."""
    return {
        "id": v.id,
        "color": v.color,
        "size": v.size,
        "material": v.material,
        "sku": v.sku,
        "price": v.price,
        "stock": v.stock,
        "is_active": v.is_active,
        "image_url": v.image_url,
    }


def _product_dict(p: Product) -> dict:
    """Serialise a Product ORM row to a plain dict for API responses."""
    variants = [v for v in (p.variants or []) if v.is_active] if p.has_variants else []
    available_colors = list(dict.fromkeys(v.color for v in variants if v.color))
    available_sizes = list(dict.fromkeys(v.size for v in variants if v.size))
    return {
        "id": p.id,
        "name": p.name,
        "sku": p.sku,
        "category": p.category,
        "price": p.price,
        "description": p.description,
        "image_url": p.image_url,
        "stock": p.stock,
        "is_active": p.is_active,
        "is_available": (p.stock or 0) > 0,
        "low_stock_alert": p.low_stock_alert,
        "has_variants": p.has_variants,
        "variants": [_variant_dict(v) for v in variants],
        "available_colors": available_colors,
        "available_sizes": available_sizes,
    }


def _business_dict(c: Client) -> dict:
    """Serialise public business fields from a Client row."""
    return {
        "name": c.business_name,
        "tagline": c.catalogue_tagline,
        "logo_url": c.logo_url,
        "banner_url": c.banner_url,
        "whatsapp_number": c.whatsapp_number,
        "instagram_id": c.instagram_account_id,
        "theme_color": c.catalogue_theme_color or "#6366F1",
        "slug": c.catalogue_slug,
    }


# ---------------------------------------------------------------------------
# GET /shop/{slug}
# ---------------------------------------------------------------------------

@router.get("/shop/{slug}")
async def get_catalogue(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Return client public info and all active products for the given catalogue slug.

    Args:
        slug: The unique catalogue identifier (e.g. "riyasarees").

    Returns:
        Dict with business info, products list, and category list.
    """
    client = await _get_client_by_slug(slug, db)

    result = await db.execute(
        select(Product)
        .where(Product.client_id == client.id, Product.is_active == True)
        .order_by(Product.created_at.desc())
    )
    products = result.scalars().all()

    categories = sorted({p.category for p in products if p.category})

    return {
        "business": _business_dict(client),
        "products": [_product_dict(p) for p in products],
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# GET /shop/{slug}/product/{sku}
# ---------------------------------------------------------------------------

@router.get("/shop/{slug}/product/{sku}")
async def get_product(slug: str, sku: str, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Return a single product detail for the given slug + SKU.

    Args:
        slug: Catalogue slug.
        sku: Product SKU code.

    Returns:
        Dict with product, business summary, and a pre-filled WhatsApp message.
    """
    client = await _get_client_by_slug(slug, db)

    result = await db.execute(
        select(Product).where(
            Product.client_id == client.id,
            Product.sku == sku,
            Product.is_active == True,
        )
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    wa_message = (
        f"Hi! I want to order:\n"
        f"Product: {product.name}\n"
        f"SKU: {product.sku}\n"
        f"Price: ₹{product.price:,.0f}\n"
        f"From: {client.business_name}"
    )

    return {
        "product": _product_dict(product),
        "business": {
            "name": client.business_name,
            "whatsapp_number": client.whatsapp_number,
            "theme_color": client.catalogue_theme_color or "#6366F1",
        },
        "whatsapp_message": wa_message,
    }


# ---------------------------------------------------------------------------
# GET /shop/{slug}/search
# ---------------------------------------------------------------------------

@router.get("/shop/{slug}/search")
async def search_products(
    slug: str,
    q: str = Query(default="", description="Search query"),
    category: Optional[str] = Query(default=None, description="Filter by category"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Search active products by name, SKU, or description. Optionally filter by category.

    Args:
        slug: Catalogue slug.
        q: Free-text search query.
        category: Optional category filter.

    Returns:
        Dict with matching products and categories.
    """
    client = await _get_client_by_slug(slug, db)

    stmt = select(Product).where(
        Product.client_id == client.id,
        Product.is_active == True,
    )
    result = await db.execute(stmt)
    all_products = result.scalars().all()

    q_lower = q.lower().strip()
    filtered = []
    for p in all_products:
        if category and p.category != category:
            continue
        if q_lower:
            searchable = " ".join(filter(None, [p.name, p.sku, p.description, p.category])).lower()
            if q_lower not in searchable:
                continue
        filtered.append(p)

    categories = sorted({p.category for p in all_products if p.category})
    return {
        "products": [_product_dict(p) for p in filtered],
        "categories": categories,
        "total": len(filtered),
    }


# ---------------------------------------------------------------------------
# POST /shop/{slug}/notify-restock
# ---------------------------------------------------------------------------

class RestockRequest(BaseModel):
    """Request body for restock notification registration."""

    phone: str
    sku: str


@router.post("/shop/{slug}/notify-restock", status_code=status.HTTP_201_CREATED)
async def notify_restock(
    slug: str,
    body: RestockRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Register a customer phone number to be notified when a product is restocked.

    Args:
        slug: Catalogue slug.
        body: Customer phone and product SKU.

    Returns:
        Confirmation message.
    """
    client = await _get_client_by_slug(slug, db)

    result = await db.execute(
        select(Product).where(
            Product.client_id == client.id,
            Product.sku == body.sku,
            Product.is_active == True,
        )
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    notification = RestockNotification(
        client_id=client.id,
        product_id=product.id,
        customer_phone=body.phone,
    )
    db.add(notification)
    await db.commit()

    return {"message": "You'll be notified when this product is back in stock."}


# ---------------------------------------------------------------------------
# GET /shop/{slug}/pdf
# ---------------------------------------------------------------------------

@router.get("/shop/{slug}/pdf")
async def download_pdf(slug: str, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """
    Generate and stream a PDF catalogue for the given slug.

    Uses reportlab to render a styled PDF with business header and all active
    products (name, SKU, price, description, category).

    Args:
        slug: Catalogue slug.

    Returns:
        StreamingResponse with application/pdf content type.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PDF generation library not available.",
        ) from exc

    client = await _get_client_by_slug(slug, db)

    result = await db.execute(
        select(Product)
        .where(Product.client_id == client.id, Product.is_active == True)
        .order_by(Product.category, Product.name)
    )
    products = result.scalars().all()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    # Parse hex theme color to RGB tuple (0–1 range)
    hex_color = (client.catalogue_theme_color or "#6366F1").lstrip("#")
    try:
        r, g, b = (int(hex_color[i:i+2], 16) / 255 for i in (0, 2, 4))
        theme = colors.Color(r, g, b)
    except Exception:
        theme = colors.HexColor("#6366F1")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], textColor=theme, fontSize=24, spaceAfter=4)
    tag_style = ParagraphStyle("tag", parent=styles["Normal"], textColor=colors.grey, fontSize=11, spaceAfter=12)
    h2_style = ParagraphStyle("h2", parent=styles["Heading2"], textColor=theme, fontSize=13, spaceBefore=14, spaceAfter=4)
    body_style = styles["BodyText"]

    story = [
        Paragraph(client.business_name, title_style),
    ]
    if client.catalogue_tagline:
        story.append(Paragraph(client.catalogue_tagline, tag_style))
    story.append(HRFlowable(width="100%", thickness=1, color=theme, spaceAfter=12))

    # Group products by category
    by_cat: dict[str, list[Product]] = {}
    for p in products:
        cat = p.category or "General"
        by_cat.setdefault(cat, []).append(p)

    for cat, items in by_cat.items():
        story.append(Paragraph(cat, h2_style))

        table_data = [["Product", "SKU", "Price", "Stock", "Description"]]
        for p in items:
            desc = (p.description or "")[:80] + ("…" if len(p.description or "") > 80 else "")
            stock_label = "In Stock" if (p.stock or 0) > 0 else "Out of Stock"
            table_data.append([
                Paragraph(p.name, body_style),
                p.sku or "—",
                f"₹{p.price:,.0f}",
                stock_label,
                Paragraph(desc, body_style),
            ])

        col_widths = [45 * mm, 20 * mm, 22 * mm, 22 * mm, None]
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(0.96, 0.96, 0.98)]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 6 * mm))

    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceBefore=8))
    story.append(Paragraph("Powered by AgentlyAI", ParagraphStyle("footer", parent=styles["Normal"], textColor=colors.grey, fontSize=8, alignment=1)))

    doc.build(story)
    buf.seek(0)

    filename = re.sub(r"[^a-z0-9-]", "", slug) + "-catalogue.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
