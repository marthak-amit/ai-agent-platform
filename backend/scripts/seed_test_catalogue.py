"""
Bulk-seed a large, realistic textile catalogue for load / search / flow testing.

Run from the backend/ directory with the venv active:
    python scripts/seed_test_catalogue.py --client-id 1 --count 1000
    python scripts/seed_test_catalogue.py --client-id 1 --dry-run

Generation is deterministic (fixed RNG seed), so re-running adds only the
products whose name is not already in the client's catalogue. Products carry no
photos — image-recognition tests still need real uploads.
"""

import argparse
import asyncio
import os
import random
import sys

# Allow import of app.* from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.client import Client
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.catalogue_service import _CATEGORY_PREFIXES, _DEFAULT_PREFIX

RNG_SEED = 20261013

COLORS = [
    "Red", "Blue", "Green", "Pink", "Yellow", "Black", "White", "Maroon", "Purple",
    "Orange", "Gold", "Navy", "Peach", "Mustard", "Teal", "Grey", "Magenta", "Cream",
]
SIZES = ["S", "M", "L", "XL", "XXL"]

# category → (share of the total, fabrics, styles, price range ₹, colour-size variants?, delivery-days override)
SPEC: dict[str, dict] = {
    "Saree": {
        "share": 0.45,
        "fabrics": [
            "Banarasi Silk", "Kanjivaram Silk", "Georgette", "Chiffon", "Cotton", "Linen",
            "Organza", "Tussar Silk", "Crepe", "Satin", "Chanderi", "Patola Silk",
            "Bandhani", "Net",
        ],
        "styles": [
            "Zari Border", "Floral Print", "Embroidered", "Sequin Work", "Digital Print",
            "Kalamkari", "Meenakari", "Paithani", "Ikat", "Block Print", "Stone Work",
            "Gota Patti", "Woven Butta", "Temple Border", "Mirror Work", "Lace Border",
            "Ombre", "Polka Dot", "Geometric", "Peacock Motif", "Checks", "Striped",
            "Hand Painted", "Tie Dye", "Resham Work", "Cutwork", "Zardozi", "Foil Print",
            "Plain with Blouse", "Paisley", "Jaal Work", "Bridal", "Party Wear",
            "Festive", "Office Wear", "Daily Wear",
        ],
        "price": (449, 8999),
        "sized": False,
        "delivery_days": None,
    },
    "Lehenga": {
        "share": 0.15,
        "fabrics": ["Velvet", "Georgette", "Net", "Silk", "Brocade", "Satin", "Organza", "Banarasi"],
        "styles": [
            "Bridal Embroidered", "Sequin Work", "Mirror Work", "Zari Work", "Designer",
            "Garba Special", "Navratri Chaniya Choli", "Sangeet Wear", "Reception",
            "Mehendi Special", "Haldi Wear", "Floral Print", "Dori Work", "Thread Work",
            "Ruffle", "Can-Can", "Pastel", "Heavy Border", "Jacquard", "Party Wear",
        ],
        "price": (2499, 24999),
        "sized": True,
        "delivery_days": 7,
    },
    "Kurti": {
        "share": 0.20,
        "fabrics": ["Cotton", "Rayon", "Georgette", "Chanderi", "Linen", "Silk Blend", "Crepe", "Muslin"],
        "styles": [
            "A-Line", "Straight Cut", "Anarkali", "Embroidered", "Block Print", "Floral Print",
            "Chikankari", "Bandhani", "Angrakha", "Kalidar", "High-Low", "Pintucks",
            "Front Slit", "Collar Neck", "Mandarin Collar", "Pocket", "Printed Long",
            "Ethnic Motif", "Gota Patti", "Mirror Work", "Office Wear", "Casual",
            "Party Wear", "Festive", "Layered", "Tiered", "Peplum", "Co-ord",
        ],
        "price": (299, 1999),
        "sized": True,
        "delivery_days": None,
    },
    "Dupatta": {
        "share": 0.10,
        "fabrics": ["Chiffon", "Georgette", "Net", "Silk", "Cotton", "Banarasi", "Organza", "Chanderi"],
        "styles": [
            "Phulkari", "Bandhani", "Gota Patti", "Zari Border", "Embroidered", "Block Print",
            "Lace Border", "Tassel", "Mirror Work", "Ombre", "Plain", "Printed", "Sequin", "Ikat", "Kantha",
        ],
        "price": (199, 1799),
        "sized": False,
        "delivery_days": None,
    },
    "Salwar Suit": {
        "share": 0.10,
        "fabrics": ["Cotton", "Georgette", "Rayon", "Chanderi", "Muslin", "Crepe", "Silk Blend", "Linen"],
        "styles": [
            "Straight Suit", "Anarkali Suit", "Palazzo Suit", "Patiala Suit", "Churidar Suit",
            "Sharara Suit", "Punjabi Suit", "Embroidered", "Printed", "Chikankari",
            "Bandhani", "Party Wear", "Festive", "Casual", "Office Wear", "Pakistani Style",
        ],
        "price": (599, 4999),
        "sized": True,
        "delivery_days": None,
    },
}

NOUN = {"Saree": "Saree", "Lehenga": "Lehenga Choli", "Kurti": "Kurti", "Dupatta": "Dupatta", "Salwar Suit": "Suit"}

DESCRIPTION = {
    "Saree": "{fabric} saree with {style_l} design. Comes with matching blouse piece. Soft drape, suitable for {occasion}.",
    "Lehenga": "{fabric} lehenga choli set with {style_l} detailing. Includes dupatta. Semi-stitched, adjustable up to size XL. Ideal for {occasion}.",
    "Kurti": "{fabric} kurti, {style_l} style. Comfortable fit, easy to wash and wear. Great for {occasion}.",
    "Dupatta": "{fabric} dupatta with {style_l} finish. Length 2.25 m. Pairs well with suits and lehengas for {occasion}.",
    "Salwar Suit": "{fabric} salwar suit, {style_l} style. Top, bottom and dupatta included. Dress material may need stitching. For {occasion}.",
}
OCCASIONS = ["weddings", "festivals", "daily wear", "parties", "office wear", "puja", "family functions", "Navratri"]


def _round_price(raw: float) -> int:
    """Round a raw price to a seller-style figure (ends in 9, 49, 99 or 0)."""
    if raw < 1000:
        return int(raw // 10 * 10 + 9)
    if raw < 5000:
        return int(raw // 50 * 50 + 49)
    return int(raw // 100 * 100 + 99)


def build_catalogue(count: int) -> list[dict]:
    """Return `count` deterministic product dicts (unique names, unique SKUs, variants included)."""
    rng = random.Random(RNG_SEED)
    quotas = {cat: int(count * spec["share"]) for cat, spec in SPEC.items()}
    quotas["Saree"] += count - sum(quotas.values())  # rounding remainder goes to sarees

    used_skus: set[str] = set()
    products: list[dict] = []
    for category, spec in SPEC.items():
        combos = [(f, s) for f in spec["fabrics"] for s in spec["styles"]]
        if quotas[category] > len(combos):
            raise SystemExit(f"Not enough unique name combinations for {category}: need {quotas[category]}, have {len(combos)}")
        rng.shuffle(combos)
        prefix = _CATEGORY_PREFIXES.get(category.lower(), _DEFAULT_PREFIX)
        for fabric, style in combos[: quotas[category]]:
            sku = f"{prefix}{rng.randint(10000, 99999)}"
            while sku in used_skus:
                sku = f"{prefix}{rng.randint(10000, 99999)}"
            used_skus.add(sku)

            price = _round_price(rng.uniform(*spec["price"]))
            colors = rng.sample(COLORS, rng.randint(2, 5) if not spec["sized"] else rng.randint(1, 3))
            variants = []
            for color in colors:
                for size in (SIZES if spec["sized"] else [None]):
                    # ~12% of variants sold out, the rest 1–40 in stock
                    stock = 0 if rng.random() < 0.12 else rng.randint(1, 40)
                    suffix = f"-{color.upper()}" + (f"-{size}" if size else "")
                    variants.append({"color": color, "size": size, "stock": stock, "sku": f"{sku}{suffix}"})

            products.append({
                "name": f"{style} {fabric} {NOUN[category]}",
                "sku": sku,
                "category": category,
                "price": float(price),
                "description": DESCRIPTION[category].format(
                    fabric=fabric, style_l=style.lower(), occasion=rng.choice(OCCASIONS)
                ),
                "delivery_days": spec["delivery_days"],
                "is_active": rng.random() > 0.02,  # ~2% hidden, to exercise the is_active filter
                "variants": variants,
            })
    rng.shuffle(products)
    return products


async def main() -> None:
    """Parse args, insert any missing products + variants for the client, print a summary."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", help="Build and summarise, write nothing")
    args = parser.parse_args()

    catalogue = build_catalogue(args.count)

    if args.dry_run:
        print(f"[dry-run] would consider {len(catalogue)} products, {sum(len(p['variants']) for p in catalogue)} variants")
        return

    engine = create_async_engine(get_settings().database_url, echo=False)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        client = await session.get(Client, args.client_id)
        if client is None:
            raise SystemExit(f"No client with id {args.client_id}")

        taken_names = {n.lower() for n in (await session.execute(
            select(Product.name).where(Product.client_id == client.id)
        )).scalars()}
        # SKUs are checked platform-wide, matching how generate_sku() is expected to behave
        taken_skus = set((await session.execute(
            select(Product.sku).where(Product.sku.is_not(None))
        )).scalars())

        created = skipped = n_variants = 0
        first_id = last_id = None
        for p in catalogue:
            if p["name"].lower() in taken_names or p["sku"] in taken_skus:
                skipped += 1
                continue
            product = Product(
                client_id=client.id,
                name=p["name"],
                sku=p["sku"],
                category=p["category"],
                price=p["price"],
                stock=sum(v["stock"] for v in p["variants"]),
                description=p["description"],
                is_active=p["is_active"],
                has_variants=True,
                delivery_days=p["delivery_days"],
            )
            session.add(product)
            await session.flush()
            for v in p["variants"]:
                session.add(ProductVariant(
                    product_id=product.id, client_id=client.id, color=v["color"],
                    size=v["size"], sku=v["sku"], price=p["price"], stock=v["stock"],
                ))
                n_variants += 1
            created += 1
            first_id = first_id or product.id
            last_id = product.id
        await session.commit()
    await engine.dispose()

    print(f"[seed_test_catalogue] client {client.id} ({client.business_name}): "
          f"created {created} products / {n_variants} variants, skipped {skipped} existing")
    if created:
        print(f"  product ids {first_id}–{last_id}")


if __name__ == "__main__":
    asyncio.run(main())
