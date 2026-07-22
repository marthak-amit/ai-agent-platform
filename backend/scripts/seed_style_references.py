"""
Seed the style_references library for the Product Photo Enhancement feature.

Run from the backend/ directory with the venv active:
    python scripts/seed_style_references.py

Idempotent — safe to re-run; existing rows are matched by (category, style_type,
reference_image_url) and skipped, so adding a new style later is just adding a
new entry to _STYLES.

Seeds the "saree" category with 5 styles (2x dummy, 2x human_model, 1x hanging),
plus 3 "universal" human_model styles selectable regardless of product category.

IMPORTANT: reference_image_url values below are the real asset URLs the
business team supplies (hosted on their storage bucket/CDN) — not generated
by this script. If a URL doesn't resolve yet, generation calls against that
style will fail at the _load_image_bytes() fetch step; upload the actual
images to storage before relying on this style in production.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.style_reference import StyleReference

_STYLES = [
    dict(
        category="saree",
        style_type="dummy",
        reference_image_url="https://storage.../styles/saree_dummy_white_bg.jpg",
        description="Saree draped on dummy/stand, plain white background, front-facing, studio lighting",
    ),
    dict(
        category="saree",
        style_type="dummy",
        reference_image_url="https://storage.../styles/saree_dummy_studio_bg.jpg",
        description="Saree draped on dummy/stand, styled studio backdrop with soft props, front-facing",
    ),
    dict(
        category="saree",
        style_type="human_model",
        reference_image_url="https://storage.../styles/saree_model_a_studio.jpg",
        description="Saree worn by human model, studio background, front-facing, standing pose",
    ),
    dict(
        category="saree",
        style_type="human_model",
        reference_image_url="https://storage.../styles/saree_model_b_studio.jpg",
        description="Saree worn by human model (different build/tone), studio background, front-facing",
    ),
    dict(
        category="saree",
        style_type="hanging",
        reference_image_url="https://storage.../styles/saree_hanging_plain.jpg",
        description="Saree hung flat on stand/hanger, minimal plain background, full drape visible",
    ),
    # Validated in manual AI Studio testing: an AI-generated pose/model reference
    # image fuses reliably with the product garment image (no safety-filter
    # issues), so these are 'universal' — selectable for any product category,
    # not just saree.
    dict(
        category="universal",
        style_type="human_model",
        reference_image_url="https://res.cloudinary.com/qs5xlym5/image/upload/v1784055863/modal1_n80cvt.png",
        description="Studio setting, three-quarter side pose, neutral grey background, professional studio lighting, relaxed confident stance",
    ),
    dict(
        category="universal",
        style_type="human_model",
        reference_image_url="https://res.cloudinary.com/qs5xlym5/image/upload/v1784055980/modal2_mwtmkf.png",
        description="Indoor daylight, front-facing full body pose, near window, warm wooden flooring, straight confident stance",
    ),
    dict(
        category="universal",
        style_type="human_model",
        reference_image_url="https://res.cloudinary.com/qs5xlym5/image/upload/v1784055991/modal3_mievl5.png",
        description="Outdoor heritage courtyard, walking pose, golden-hour lighting, graceful natural movement",
    ),
]


async def main() -> None:
    """Insert (or skip if already present) each style reference row."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        for style in _STYLES:
            existing = await db.execute(
                select(StyleReference).where(
                    StyleReference.category == style["category"],
                    StyleReference.style_type == style["style_type"],
                    StyleReference.reference_image_url == style["reference_image_url"],
                )
            )
            if existing.scalar_one_or_none() is not None:
                print(f"skip (exists): {style['category']} / {style['style_type']} / {style['reference_image_url']}")
                continue

            db.add(StyleReference(
                category=style["category"],
                style_type=style["style_type"],
                reference_image_url=style["reference_image_url"],
                description=style["description"],
                active=True,
            ))
            print(f"created: {style['category']} / {style['style_type']} / {style['reference_image_url']}")

        await db.commit()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
