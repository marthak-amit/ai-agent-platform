"""
Realistic test-data seed script for local development.

Run from the backend/ directory with the venv active:
    python seed.py

Idempotent — safe to re-run; existing rows are skipped by email / phone_number.

Creates:
  • 1 test client    — Riya Sarees Surat (growth plan)
  • 5 products       — linked to that client
  • 11 conversations — mix of hot / warm / cold leads with Hindi/Hinglish/Gujarati messages
  • Usage logs       — 7 days of realistic daily counts
  • 1 admin account  — admin@test.com / admin123
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.knowledge_base import KnowledgeBase
from app.models.usage_log import UsageLog
from app.services.auth_service import hash_password
from app.services.onboarding_service import generate_api_key, generate_system_prompt

# ── Seed constants ─────────────────────────────────────────────────────────────

_CLIENT = dict(
    email="riya@riyasarees.com",
    password="riya1234",
    business_name="Riya Sarees Surat",
    business_type="textile",
    business_description=(
        "We are a premium saree boutique based in Surat, Gujarat. "
        "We specialise in Banarasi silk, Kanjivaram, Georgette, and Cotton sarees "
        "for weddings, festivals, and everyday wear. Pan-India delivery available."
    ),
    whatsapp_number="919876543210",
    plan_slug="growth",
    daily_message_limit=300,
    catalogue_slug="riyasarees",
    catalogue_tagline="Premium Banarasi Sarees — Direct from Surat",
    upi_id="riyasarees@paytm",
    accepts_cod=False,
)

_ADMIN = dict(email="admin@test.com", password="admin123", business_name="Admin")

_PRODUCTS = [
    dict(name="Banarasi Silk Saree",    sku="SR27754", category="Saree",   price=2450.0, stock=18, description="Pure Banarasi silk with gold zari work. Perfect for weddings and festivals."),
    dict(name="Kanjivaram Silk Saree",  sku="SR29821", category="Saree",   price=4200.0, stock=10, description="Authentic South Indian Kanjivaram in vibrant colours with temple border."),
    dict(name="Georgette Party Wear",   sku="SR31045", category="Saree",   price=1100.0, stock=35, description="Lightweight georgette saree with embroidery. Ideal for parties and evening events."),
    dict(name="Cotton Printed Saree",   sku="SR33210", category="Saree",   price=850.0,  stock=50, description="Breathable pure cotton saree in floral prints. Machine washable and everyday-friendly."),
    dict(name="Designer Lehenga",       sku="LH10042", category="Lehenga", price=6500.0, stock=7,  description="Heavily embroidered bridal lehenga choli set. Available in red, pink, and navy."),
]

# Each entry: (product_name, variant_specs)
# variant_specs is either:
#   - {color: stock}                 — colour-only variants (e.g. sarees, no sizes)
#   - {color: {size: stock}}         — colour x size variants (e.g. lehenga, party wear)
_VARIANT_PRODUCTS: list[tuple[str, dict]] = [
    ("Banarasi Silk Saree", {
        "Red": 8,
        "Blue": 5,
        "Green": 3,
        "Pink": 2,
    }),
    ("Kanjivaram Silk Saree", {
        "Gold": 4,
        "Red": 3,
        "Green": 2,
        "Purple": 1,
    }),
    ("Georgette Party Wear", {
        "Black": {"S": 5, "M": 8, "L": 6, "XL": 3, "XXL": 1},
        "Red":   {"S": 3, "M": 5, "L": 4, "XL": 2, "XXL": 0},
        "Blue":  {"S": 4, "M": 6, "L": 3, "XL": 1, "XXL": 0},
    }),
    ("Designer Lehenga", {
        "Red":  {"S": 3, "M": 5, "L": 4, "XL": 2, "XXL": 0},
        "Pink": {"S": 5, "M": 3, "L": 2, "XL": 1, "XXL": 0},
        "Navy": {"S": 2, "M": 4, "L": 3, "XL": 2, "XXL": 1},
    }),
]

# (phone, channel, lead_status, [(role, content), ...])
_NOW = datetime.now(timezone.utc)

_CONVERSATIONS = [
    # ── HOT leads ──────────────────────────────────────────────────────────────
    (
        "919912345678", "whatsapp", "hot",
        _NOW - timedelta(hours=2),
        [
            ("user",  "Namaste! Banarasi silk saree chahiye. Kya stock hai?"),
            ("model", "Namaste ji! Bilkul, humare paas beautiful Banarasi silk sarees available hain ✨ ₹2450 mein ek gorgeous piece hai jisme pure silk aur gold zari work hai. Kya aap colour aur design ke baare mein batayengi?"),
            ("user",  "Red ya maroon mein chahiye. Shaadi ke liye hai."),
            ("model", "Shaadi ke liye bilkul perfect rahegi! Humare paas red aur maroon dono available hain — pure silk, heavy zari border ke saath 💕 Kya main aapka order confirm kar sakti hun?"),
            ("user",  "Haan, order karna hai. COD available hai?"),
            ("model", "Zaroor! COD available hai 😊 Apna delivery address aur naam share karein, hum 3–5 business days mein deliver kar denge."),
        ],
    ),
    (
        "919823456789", "whatsapp", "hot",
        _NOW - timedelta(hours=5),
        [
            ("user",  "Hello, Designer Lehenga ka price kya hai?"),
            ("model", "Namaste! Humare Designer Lehenga ki price ₹6500 hai. Heavy embroidery work ke saath bahut stunning lagta hai 🎊 Kisi special occasion ke liye hai?"),
            ("user",  "Haan meri beti ki engagement hai next month. Discount milega?"),
            ("model", "Engagement ke liye heartiest congratulations! 🎉 Aapke liye hum special 5% discount de sakte hain — final price ₹6175 hogi. Gift wrapping bhi free mein."),
            ("user",  "Ok deal hai. Payment kaise karna hai?"),
            ("model", "Bank transfer ya UPI dono accept karte hain. Mujhe apna naam aur address WhatsApp par bhejein, main order confirm karti hun 🙏"),
        ],
    ),
    (
        "919734567890", "whatsapp", "hot",
        _NOW - timedelta(hours=8),
        [
            ("user",  "Aapka Kanjivaram silk saree available hai kya?"),
            ("model", "Haan ji! Humare paas authentic Kanjivaram silk sarees hain, ₹4200 se starting 🌟 Temple borders aur real gold zari work ke saath — bilkul genuine piece. Kya dekhna chahenge?"),
            ("user",  "4200 wala ready to ship hai kya? Aaj order karun toh kab milega?"),
            ("model", "Bilkul ready hai! Aaj order karo toh 2–3 business days mein dispatch ho jaayega. Bangalore tak normally 5 days mein pahuunch jaati hai."),
            ("user",  "Book karo bhai. Bangalore deliver hoga na?"),
            ("model", "100% Bangalore delivery hoti hai! 📦 Shipping charge ₹150 extra hogi, total ₹4350. Naam aur address bhejein — abhi book kar dete hain."),
        ],
    ),
    (
        "919645678901", "whatsapp", "hot",
        _NOW - timedelta(hours=14),
        [
            ("user",  "Urgent hai yaar. Georgette saree kal tak mil sakti hai kya?"),
            ("model", "Urgent delivery ke liye express shipping available hai! Extra ₹300 charges honge. Kaunsa colour chahiye Georgette mein?"),
            ("user",  "Pink ya peach. Georgette party wear wali. Price?"  ),
            ("model", "Georgette Party Wear ₹1100 + ₹300 express = ₹1400 total 🚀 Pink aur peach dono available hain. Kal dopahar 2 baje se pehle order karo toh kal shaam tak deliver ho jaayegi."),
            ("user",  "Perfect! Pink wali chahiye. Order karo."),
            ("model", "Done! Abhi book kar rahi hun 🎀 Apna complete address aur pincode bhejein, toh shipping confirm karte hain."),
        ],
    ),
    # ── WARM leads ─────────────────────────────────────────────────────────────
    (
        "919556789012", "whatsapp", "warm",
        _NOW - timedelta(days=1),
        [
            ("user",  "Cotton printed sarees kaunse colours mein aati hain?"),
            ("model", "Cotton printed sarees bahut saare colours mein available hain — pink, blue, green, yellow, orange! ₹850 mein pure cotton, machine washable, daily wear ke liye perfect 🌸 Kaunsa colour pasand hai?"),
            ("user",  "Pink wali dikhao na please. Koi image hai?"),
            ("model", "Pink cotton mein light pink floral print aur dark pink geometric dono options hain. Dono equally popular hain! Catalogue link yahan hai: wa.me/919876543210 — seedha WhatsApp par image bhej sakti hun."),
            ("user",  "Sochti hun... COD available hai?"),
            ("model", "Haan COD bilkul available hai! 5–7 days delivery, koi advance payment nahi 😊 Jab bhi decide karo, main yahin hun!"),
        ],
    ),
    (
        "919467890123", "whatsapp", "warm",
        _NOW - timedelta(days=2),
        [
            ("user",  "Yeh sarees genuine silk hain ya synthetic?"),
            ("model", "Humare Banarasi aur Kanjivaram 100% pure silk hain — har piece ke saath authenticity tag attached hota hai 🏅 Georgette aur Cotton natural fabric hain. Kaunsi saree ke baare mein poochh rahi hain?"),
            ("user",  "Wholesale mein milti hain? Main boutique chalati hun."),
            ("model", "Wholesale pricing available hai! Minimum 10 pieces per style par bulk discount milti hai. Boutique owners ke liye special rates bhi hain. Aapko detailed price list bhejun?"),
            ("user",  "Haan bhejo. Aur samples mil sakte hain?"),
            ("model", "Price list WhatsApp par bhej rahi hun. Sample swatches ₹200 mein courier se bhej sakte hain, jo first order par adjust ho jaata hai 🙏"),
        ],
    ),
    (
        "919378901234", "whatsapp", "warm",
        _NOW - timedelta(days=3),
        [
            ("user",  "Meri sister ki shaadi hai. Kya 2000 budget mein kuch milega?"),
            ("model", "₹2000 budget mein Georgette Party Wear (₹1100) ya Cotton Printed Saree (₹850) — dono beautiful options hain! Shaadi ke liye Georgette zyada elegant lagti hai 💐"),
            ("user",  "Kanjivaram nahi milegi is budget mein?"),
            ("model", "Kanjivaram ₹4200 hai, unfortunately budget se thoda bahar hai. Lekin agar stretch kar sakti hain ₹2450 tak, toh Banarasi Silk Saree ek stunning choice hogi shaadi ke liye!"),
            ("user",  "Hmm. Sochti hun."),
        ],
    ),
    # ── COLD leads ─────────────────────────────────────────────────────────────
    (
        "919289012345", "whatsapp", "cold",
        _NOW - timedelta(days=4),
        [
            ("user",  "Sarees bechte ho?"),
            ("model", "Haan ji! Riya Sarees Surat mein wide range available hai — Banarasi silk, Kanjivaram, Georgette, Cotton aur Designer Lehenga 🎉 Kaunsi saree mein interest hai?"),
            ("user",  "Ok"),
        ],
    ),
    (
        "919190123456", "whatsapp", "cold",
        _NOW - timedelta(days=6),
        [
            ("user",  "Price kya hai sarees ka?"),
            ("model", "Humare prices: Cotton Printed ₹850 | Georgette ₹1100 | Banarasi Silk ₹2450 | Kanjivaram Silk ₹4200 | Designer Lehenga ₹6500 ✨ Kaunsi saree pasand aayi?"),
        ],
    ),
    (
        "919001234567", "instagram", "cold",
        _NOW - timedelta(days=7),
        [
            ("user",  "Kya aap delivery karte ho bahar bhi?"),
            ("model", "Haan, hum pan-India delivery karte hain! 🚚 Standard 5–7 days, express 1–2 days available hai. International shipping bhi hoti hai. Aap kahan se hain?"),
            ("user",  "Pune se hun."),
            ("model", "Pune mein 4–5 days mein deliver ho jaati hai! Standard shipping ₹150. Koi saree dekhni ho toh zaroor batayein 😊"),
        ],
    ),
    # ── Gujarati lead ──────────────────────────────────────────────────────────
    (
        "919876100001", "whatsapp", "warm",
        _NOW - timedelta(hours=3),
        [
            ("user",  "kem cho, aa banarasi saree ni kimat ketli che?"),
            ("model", "Kem cho! Banarasi Silk Saree ni kimat ₹2450 che. Pure silk saree chhe, gold zari work sathe — bilkul sundar! Stock available che. Order lai shakay cho? 🙏"),
            ("user",  "stock available che?"),
            ("model", "Haa ji, 18 pieces stock mein available che! Kem, order karvanu chhe? Colour aur size batao toh tarant book kari aapiye."),
            ("user",  "order kai rite karvanu?"),
            ("model", "Order karva mate tamaro naam, address aur pincode WhatsApp par moklo. COD aur UPI — be payment options available che. Delivery 3-5 days mein thaay che. Aabhaar! 🎉"),
        ],
    ),
]

_USAGE_DAYS = [
    (0, 42), (1, 31), (2, 58), (3, 27), (4, 63), (5, 19), (6, 44),
]

# (order_number, customer_name, customer_phone, product_name, product_sku,
#  qty, unit_price, payment_method, status, days_ago,
#  variant_color, variant_size, tracking_number, courier_name, delivery_address)
_ORDERS = [
    # New orders — today
    ("ORD-2026-0001", "Priya Mehta", "919876500001", "Banarasi Silk Saree", "SR27754",
     1, 2450.0, "COD", "new", 0, "Red", None, None, None,
     "12, Laxmi Nagar, Surat, Gujarat 395001"),
    ("ORD-2026-0002", "Sunita Sharma", "919876500002", "Kanjivaram Silk Saree", "SR29821",
     1, 4200.0, "UPI", "new", 0, "Gold", None, None, None,
     "34B, Shastri Colony, Ahmedabad, Gujarat 380001"),
    # Confirmed orders — today
    ("ORD-2026-0003", "Reena Patel", "919876500003", "Designer Lehenga", "LH10042",
     1, 6500.0, "COD", "confirmed", 0, "Red", "M", None, None,
     "67, Civil Lines, Jaipur, Rajasthan 302001"),
    ("ORD-2026-0004", "Kavita Joshi", "919876500004", "Georgette Party Wear", "SR31045",
     2, 1100.0, "COD", "confirmed", 0, "Black", "L", None, None,
     "22, MG Road, Bangalore, Karnataka 560001"),
    ("ORD-2026-0005", "Anita Gupta", "919876500005", "Cotton Printed Saree", "SR33210",
     3, 850.0, "UPI", "confirmed", 0, None, None, None, None,
     "9, Nehru Street, Chennai, Tamil Nadu 600001"),
    # Dispatched — yesterday
    ("ORD-2026-0006", "Meena Verma", "919876500006", "Banarasi Silk Saree", "SR27754",
     1, 2450.0, "COD", "dispatched", 1, "Blue", None, "DL123456789", "Delhivery",
     "45, Park Avenue, Mumbai, Maharashtra 400001"),
    ("ORD-2026-0007", "Seema Rao", "919876500007", "Kanjivaram Silk Saree", "SR29821",
     1, 4200.0, "UPI", "dispatched", 1, "Purple", None, "BD987654321", "BlueDart",
     "78, Lake View, Hyderabad, Telangana 500001"),
    # Delivered — last week
    ("ORD-2026-0008", "Pooja Singh", "919876500008", "Designer Lehenga", "LH10042",
     1, 6500.0, "UPI", "delivered", 7, "Pink", "S", "EP567891234IN", "India Post",
     "33, Shyam Nagar, Delhi, Delhi 110001"),
    ("ORD-2026-0009", "Nisha Kumar", "919876500009", "Georgette Party Wear", "SR31045",
     1, 1100.0, "COD", "delivered", 8, "Red", "M", "EK112233445", "Ekart",
     "15, Rajaji Nagar, Pune, Maharashtra 411001"),
    # Cancelled
    ("ORD-2026-0010", "Rita Yadav", "919876500010", "Cotton Printed Saree", "SR33210",
     2, 850.0, "COD", "cancelled", 3, None, None, None, None,
     "55, Gandhi Road, Nagpur, Maharashtra 440001"),
]

# ── Helper: get-or-create ──────────────────────────────────────────────────────

async def _get_or_create_client(
    session: AsyncSession, email: str, **kwargs
) -> tuple[Client, bool]:
    result = await session.execute(select(Client).where(Client.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        # Always sync upi_id and accepts_cod so re-seeding picks up changes.
        for field in ("upi_id", "accepts_cod"):
            if field in kwargs:
                setattr(existing, field, kwargs[field])
        return existing, False
    client = Client(email=email, **kwargs)
    session.add(client)
    await session.flush()
    return client, True


async def _get_or_create_conversation(
    session: AsyncSession, phone_number: str, channel: str, created_at: datetime
) -> tuple[Conversation, bool]:
    result = await session.execute(
        select(Conversation).where(
            Conversation.phone_number == phone_number,
            Conversation.channel == channel,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False
    conv = Conversation(phone_number=phone_number, channel=channel, created_at=created_at)
    session.add(conv)
    await session.flush()
    return conv, True


async def _get_or_create_lead(
    session: AsyncSession, phone_number: str, conversation_id: int, status: str
) -> tuple[Lead, bool]:
    result = await session.execute(
        select(Lead).where(Lead.phone_number == phone_number)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False
    lead = Lead(
        phone_number=phone_number,
        conversation_id=conversation_id,
        status=status,
    )
    session.add(lead)
    await session.flush()
    return lead, True


# ── Seed functions ─────────────────────────────────────────────────────────────

async def seed_client(session: AsyncSession) -> Client:
    products_json = [
        {"name": p["name"], "price": p["price"], "stock": p["stock"]}
        for p in _PRODUCTS
    ]
    system_prompt = generate_system_prompt(
        business_type=_CLIENT["business_type"],
        business_name=_CLIENT["business_name"],
        business_description=_CLIENT["business_description"],
        products=products_json,
    )
    client, created = await _get_or_create_client(
        session,
        email=_CLIENT["email"],
        hashed_password=hash_password(_CLIENT["password"]),
        business_name=_CLIENT["business_name"],
        business_type=_CLIENT["business_type"],
        business_description=_CLIENT["business_description"],
        whatsapp_number=_CLIENT["whatsapp_number"],
        products=products_json,
        api_key=generate_api_key(),
        plan_slug=_CLIENT["plan_slug"],
        daily_message_limit=_CLIENT["daily_message_limit"],
        gemini_system_prompt=system_prompt,
        catalogue_slug=_CLIENT["catalogue_slug"],
        catalogue_tagline=_CLIENT["catalogue_tagline"],
        upi_id=_CLIENT.get("upi_id"),
        accepts_cod=_CLIENT.get("accepts_cod", False),
    )
    tag = "created" if created else "already exists"
    print(f"  client       id={client.id:<4}  [{tag}]  {client.business_name}")
    return client


async def seed_products(session: AsyncSession, client: Client) -> list[Product]:
    created_products = []
    for p in _PRODUCTS:
        result = await session.execute(
            select(Product).where(
                Product.client_id == client.id,
                Product.name == p["name"],
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            print(f"  product      id={existing.id:<4}  [already exists]  {existing.name}")
            created_products.append(existing)
            continue
        product = Product(
            client_id=client.id,
            name=p["name"],
            sku=p.get("sku"),
            category=p.get("category"),
            price=p["price"],
            stock=p["stock"],
            description=p["description"],
        )
        session.add(product)
        await session.flush()
        print(f"  product      id={product.id:<4}  [created]         {product.name}  ₹{product.price:.0f}")
        created_products.append(product)
    return created_products


async def _upsert_variant(
    session: AsyncSession,
    client: Client,
    product: Product,
    color: str,
    size: Optional[str],
    stock: int,
) -> ProductVariant:
    """Find an existing colour/size variant row or create a new one."""
    conditions = [
        ProductVariant.product_id == product.id,
        ProductVariant.color == color,
    ]
    conditions.append(ProductVariant.size == size if size is not None else ProductVariant.size.is_(None))
    sku_suffix = f"-{color.upper()}" + (f"-{size}" if size else "")
    variant_sku = f"{product.sku}{sku_suffix}" if product.sku else None

    result = await session.execute(select(ProductVariant).where(*conditions))
    existing = result.scalar_one_or_none()
    if existing:
        existing.stock = stock
        existing.sku = variant_sku
        return existing

    variant = ProductVariant(
        product_id=product.id,
        client_id=client.id,
        color=color,
        size=size,
        sku=variant_sku,
        price=product.price,
        stock=stock,
    )
    session.add(variant)
    return variant


async def seed_variants(session: AsyncSession, client: Client, products: list[Product]) -> list[ProductVariant]:
    """Create colour/size variant rows for the products listed in _VARIANT_PRODUCTS."""
    by_name = {p.name: p for p in products}
    created_variants = []
    for product_name, color_specs in _VARIANT_PRODUCTS:
        product = by_name[product_name]
        product.has_variants = True
        for color, spec in color_specs.items():
            if isinstance(spec, dict):
                for size, stock in spec.items():
                    created_variants.append(
                        await _upsert_variant(session, client, product, color, size, stock)
                    )
            else:
                created_variants.append(
                    await _upsert_variant(session, client, product, color, None, spec)
                )
    await session.flush()
    print(f"  variants     {len(created_variants):<4}  [for {len(_VARIANT_PRODUCTS)} products]")
    return created_variants


async def seed_conversations(
    session: AsyncSession,
) -> list[tuple[Conversation, Lead]]:
    results = []
    for phone, channel, status, conv_time, turns in _CONVERSATIONS:
        conv, conv_created = await _get_or_create_conversation(
            session, phone, channel, conv_time
        )
        conv_tag = "created" if conv_created else "exists "

        if conv_created:
            # Add messages at staggered timestamps
            for i, (role, content) in enumerate(turns):
                msg = Message(
                    conversation_id=conv.id,
                    role=role,
                    content=content,
                    created_at=conv_time + timedelta(minutes=i * 3),
                )
                session.add(msg)
            await session.flush()

        lead, lead_created = await _get_or_create_lead(
            session, phone, conv.id, status
        )
        lead_tag = "created" if lead_created else "exists "
        print(
            f"  conversation id={conv.id:<4}  [{conv_tag}]  "
            f"lead={lead.status:<4} ({lead_tag})  "
            f"{phone}  {len(turns)} messages"
        )
        results.append((conv, lead))
    return results


async def seed_usage_logs(session: AsyncSession, client: Client) -> None:
    today = date.today()
    for days_ago, count in _USAGE_DAYS:
        log_date = today - timedelta(days=days_ago)
        result = await session.execute(
            select(UsageLog).where(
                UsageLog.client_id == client.id,
                UsageLog.date == log_date,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            continue
        log = UsageLog(client_id=client.id, date=log_date, message_count=count)
        session.add(log)
    await session.flush()
    total = sum(c for _, c in _USAGE_DAYS)
    print(f"  usage logs   7 days  total={total} messages")


async def seed_orders(session: AsyncSession, client: Client) -> list[Order]:
    """Seed 10 sample orders covering all status values."""
    created_orders = []
    for (
        order_number, customer_name, customer_phone, product_name, product_sku,
        qty, unit_price, payment_method, order_status, days_ago,
        variant_color, variant_size, tracking_number, courier_name, delivery_address,
    ) in _ORDERS:
        result = await session.execute(
            select(Order).where(Order.order_number == order_number)
        )
        existing = result.scalar_one_or_none()
        if existing:
            print(f"  order        {order_number:<20}  [already exists]")
            created_orders.append(existing)
            continue

        created_at = _NOW - timedelta(days=days_ago)
        order = Order(
            order_number=order_number,
            client_id=client.id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            delivery_address=delivery_address,
            product_name=product_name,
            product_sku=product_sku,
            variant_color=variant_color,
            variant_size=variant_size,
            quantity=qty,
            unit_price=unit_price,
            total_amount=unit_price * qty,
            payment_method=payment_method,
            payment_status="paid" if order_status in ("delivered", "dispatched") and payment_method == "UPI" else "pending",
            status=order_status,
            tracking_number=tracking_number,
            courier_name=courier_name,
            confirmed_at=created_at + timedelta(hours=1) if order_status != "new" else None,
            dispatched_at=created_at + timedelta(hours=12) if order_status in ("dispatched", "delivered") else None,
            delivered_at=created_at + timedelta(days=4) if order_status == "delivered" else None,
            created_at=created_at,
        )
        session.add(order)
        await session.flush()
        print(f"  order        {order_number:<20}  [created]  {order_status:<12}  ₹{order.total_amount:.0f}")
        created_orders.append(order)
    return created_orders


async def seed_customers(session: AsyncSession, client: Client) -> list[Customer]:
    """
    Seed customer profiles for the 10 seeded orders.

    Mix: 3 VIP, 4 regular, 3 new (no orders yet).
    Phones match the _ORDERS list so stats are realistic.
    """
    _CUSTOMER_RECORDS = [
        # ── VIP customers (high spend, multiple orders) ────────────────────────
        dict(
            phone="919876500008", name="Pooja Singh",
            email="pooja.singh@gmail.com",
            address="33, Shyam Nagar, Delhi, Delhi 110001",
            total_orders=3, total_spent=19500.0,
            preferred_language="hindi", preferred_payment="UPI",
            is_vip=True, tags="vip,regular",
            notes="Bridal season shopper. Prefers premium sarees.",
        ),
        dict(
            phone="919876500002", name="Sunita Sharma",
            email="sunita.sharma@gmail.com",
            address="34B, Shastri Colony, Ahmedabad, Gujarat 380001",
            total_orders=2, total_spent=8400.0,
            preferred_language="gujarati", preferred_payment="UPI",
            is_vip=True, tags="vip,wholesale",
            notes="Boutique owner. Wants bulk pricing.",
        ),
        dict(
            phone="919876500003", name="Reena Patel",
            email=None,
            address="67, Civil Lines, Jaipur, Rajasthan 302001",
            total_orders=2, total_spent=13000.0,
            preferred_language="hindi", preferred_payment="COD",
            is_vip=True, tags="vip,regular",
            notes="Wedding season buyer. Size preference: M.",
        ),
        # ── Regular customers (1–2 orders) ─────────────────────────────────────
        dict(
            phone="919876500001", name="Priya Mehta",
            email=None, address="12, Laxmi Nagar, Surat, Gujarat 395001",
            total_orders=1, total_spent=2450.0,
            preferred_language="gujarati", preferred_payment="COD",
            is_vip=False, tags="regular", notes=None,
        ),
        dict(
            phone="919876500004", name="Kavita Joshi",
            email=None, address="22, MG Road, Bangalore, Karnataka 560001",
            total_orders=1, total_spent=2200.0,
            preferred_language="english", preferred_payment="COD",
            is_vip=False, tags="regular", notes=None,
        ),
        dict(
            phone="919876500006", name="Meena Verma",
            email=None, address="45, Park Avenue, Mumbai, Maharashtra 400001",
            total_orders=1, total_spent=2450.0,
            preferred_language="hindi", preferred_payment="COD",
            is_vip=False, tags="regular", notes=None,
        ),
        dict(
            phone="919876500007", name="Seema Rao",
            email=None, address="78, Lake View, Hyderabad, Telangana 500001",
            total_orders=1, total_spent=4200.0,
            preferred_language="english", preferred_payment="UPI",
            is_vip=False, tags="regular", notes=None,
        ),
        # ── New customers (no orders, just browsed) ────────────────────────────
        dict(
            phone="919876500005", name="Anita Gupta",
            email=None, address=None,
            total_orders=0, total_spent=0.0,
            preferred_language="hindi", preferred_payment=None,
            is_vip=False, tags=None, notes=None,
        ),
        dict(
            phone="919876500009", name="Nisha Kumar",
            email=None, address=None,
            total_orders=0, total_spent=0.0,
            preferred_language="english", preferred_payment=None,
            is_vip=False, tags=None, notes=None,
        ),
        dict(
            phone="919876500010", name="Rita Yadav",
            email=None, address=None,
            total_orders=0, total_spent=0.0,
            preferred_language="hindi", preferred_payment=None,
            is_vip=False, tags=None, notes=None,
        ),
    ]

    created_customers = []
    now = datetime.now(timezone.utc)
    for rec in _CUSTOMER_RECORDS:
        result = await session.execute(
            select(Customer).where(
                Customer.client_id == client.id,
                Customer.phone == rec["phone"],
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            print(f"  customer     phone={rec['phone']:<16}  [already exists]  {rec['name']}")
            created_customers.append(existing)
            continue

        cust = Customer(
            client_id=client.id,
            phone=rec["phone"],
            name=rec["name"],
            email=rec.get("email"),
            address=rec.get("address"),
            total_orders=rec["total_orders"],
            total_spent=rec["total_spent"],
            last_order_at=now - timedelta(days=1) if rec["total_orders"] > 0 else None,
            first_message_at=now - timedelta(days=10),
            last_message_at=now - timedelta(hours=2),
            preferred_language=rec["preferred_language"],
            preferred_payment=rec.get("preferred_payment"),
            is_vip=rec["is_vip"],
            is_blocked=False,
            tags=rec.get("tags"),
            notes=rec.get("notes"),
        )
        session.add(cust)
        await session.flush()
        vip_flag = " ⭐ VIP" if rec["is_vip"] else ""
        print(f"  customer     phone={rec['phone']:<16}  [created]{vip_flag}  {rec['name']}")
        created_customers.append(cust)

    return created_customers


async def seed_admin(session: AsyncSession) -> Client:
    admin, created = await _get_or_create_client(
        session,
        email=_ADMIN["email"],
        hashed_password=hash_password(_ADMIN["password"]),
        business_name=_ADMIN["business_name"],
        plan_slug="starter",
        daily_message_limit=100,
    )
    tag = "created" if created else "already exists"
    print(f"  admin user   id={admin.id:<4}  [{tag}]  {admin.email}")
    return admin


_DEFAULT_KB = [
    {
        "question": "delivery time how many days",
        "answer": "We deliver pan-India in 3-5 business days. Express delivery available in Surat same day.",
        "category": "delivery",
    },
    {
        "question": "return policy refund exchange damaged wrong item",
        "answer": "We accept returns within 7 days of delivery for damaged or wrong items. Contact us on WhatsApp with a photo.",
        "category": "policy",
    },
    {
        "question": "wholesale bulk order discount 10 pieces",
        "answer": "For bulk orders of 10+ pieces we offer 10% discount. Contact us for wholesale pricing.",
        "category": "pricing",
    },
    {
        "question": "cash on delivery COD available payment",
        "answer": "We currently accept UPI payments only. Pay via GPay, PhonePe or Paytm.",
        "category": "payment",
    },
    {
        "question": "track order status where is my parcel",
        "answer": "After dispatch we send a tracking link via WhatsApp. Orders are dispatched within 24 hours of payment confirmation.",
        "category": "delivery",
    },
    {
        "question": "size chart measurement guide which size",
        "answer": "For sarees one size fits all. For lehengas and kurtis sizes are S/M/L/XL/XXL. Share your bust/waist measurements and we will help you pick.",
        "category": "product",
    },
]


async def seed_knowledge_base(
    session: AsyncSession, client: Client
) -> list[KnowledgeBase]:
    """Insert default KB entries for Riya Sarees; skip if already present."""
    added = []
    for kb in _DEFAULT_KB:
        existing = await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.client_id == client.id,
                KnowledgeBase.question == kb["question"],
            )
        )
        if existing.scalar_one_or_none():
            print(f"  KB entry already exists — skipping: {kb['question'][:50]}")
            continue
        entry = KnowledgeBase(
            client_id=client.id,
            question=kb["question"],
            answer=kb["answer"],
            category=kb.get("category"),
            source="manual",
        )
        session.add(entry)
        added.append(entry)
        print(f"  KB entry added : {kb['question'][:60]}")
    return added


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print()
    print("━" * 60)
    print("  Riya Sarees Surat — Seed Script")
    print("━" * 60)

    async with Session() as session:
        async with session.begin():
            print("\n[1/7] Test client")
            client = await seed_client(session)

            print("\n[2/7] Products")
            products = await seed_products(session, client)

            print("\n[3/7] Product variants")
            await seed_variants(session, client, products)

            print("\n[4/7] Conversations + leads")
            pairs = await seed_conversations(session)

            print("\n[5/7] Usage logs")
            await seed_usage_logs(session, client)

            print("\n[6/7] Sample orders")
            sample_orders = await seed_orders(session, client)

            print("\n[7/8] Customer profiles")
            sample_customers = await seed_customers(session, client)

            print("\n[8/9] Knowledge base")
            kb_entries = await seed_knowledge_base(session, client)

            print("\n[9/9] Admin account")
            admin = await seed_admin(session)

    await engine.dispose()

    # ── Summary ────────────────────────────────────────────────────────────────
    hot  = sum(1 for _, l in pairs if l.status == "hot")
    warm = sum(1 for _, l in pairs if l.status == "warm")
    cold = sum(1 for _, l in pairs if l.status == "cold")

    vip_c  = sum(1 for c in sample_customers if c.is_vip)
    new_c  = sum(1 for c in sample_customers if c.total_orders == 0)
    reg_c  = len(sample_customers) - vip_c - new_c

    print()
    print("━" * 60)
    print("  Done — all IDs")
    print("━" * 60)
    print(f"  Client id       : {client.id}")
    print(f"  Product ids     : {[p.id for p in products]}")
    print(f"  Conversation ids: {[c.id for c, _ in pairs]}")
    print(f"  Lead ids        : {[l.id for _, l in pairs]}")
    print(f"  Lead breakdown  : {hot} hot  {warm} warm  {cold} cold")
    print(f"  Orders seeded   : {len(sample_orders)}")
    print(f"  Customers seeded: {len(sample_customers)}  ({vip_c} VIP  {reg_c} regular  {new_c} new)")
    print(f"  KB entries      : {len(kb_entries)} added")
    print(f"  Admin id        : {admin.id}")
    print()
    print("  ── Login credentials ──────────────────────────────")
    print(f"  Test client   {_CLIENT['email']} / {_CLIENT['password']}")
    print(f"  Admin account {_ADMIN['email']} / {_ADMIN['password']}")
    print()
    print("  ── Admin panel ────────────────────────────────────")
    print(f"  X-Admin-Key: {settings.admin_secret_key}")
    print(f"  GET http://localhost:8000/admin/clients")
    print()


if __name__ == "__main__":
    asyncio.run(main())
