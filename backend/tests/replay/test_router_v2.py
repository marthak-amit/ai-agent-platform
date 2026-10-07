"""
Golden replay tests for ROUTER_V2 (the LLM intent router).

Real webhook → pipeline → Postgres; only the LLM is replaced by a scripted fake that returns fixed router
JSON per customer message. Each test asserts (1) the action the engine executed, (2) the DB end-state, and
(3) that no fact the LLM was handed (a hallucinated date/price in `reply_hint`) reaches the customer —
engine facts come from DB rows and templates only.

Plus one optional live test (`@pytest.mark.live`, needs GROQ_API_KEY and RUN_LIVE_LLM=1).
Runs against real Postgres (skipped automatically when it is not reachable).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all, send_message

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

AI_SENTINEL = "__AI_REPLY__"
POISON = "HALLUCINATED-FACT"      # put in reply_hint for actions whose text must come from the engine


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


# ── scripted LLM ─────────────────────────────────────────────────────────────

class ScriptedRouter:
    """
    Stands in for llm_client.chat_json: returns the scripted router JSON for the customer's message.

    A message with no script entry fails the test (the router must not be called for fast paths).
    """

    def __init__(self) -> None:
        """Start with an empty script."""
        self.script: dict[str, dict | Exception] = {}
        self.calls: list[dict] = []

    def when(self, message: str, action: str, args: dict | None = None, *, confidence: float = 0.95,
             language: str = "en", reply_hint: str = "") -> None:
        """Script the decision for an exact customer message."""
        self.script[message.strip().lower()] = {
            "action": action, "args": args or {}, "language": language,
            "confidence": confidence, "reply_hint": reply_hint,
        }

    def fails(self, message: str, exc: Exception) -> None:
        """Script an exception (e.g. LLMUnavailableError) for a message."""
        self.script[message.strip().lower()] = exc

    async def __call__(self, model, messages, *, max_tokens, purpose="json", client_id=None, conversation_id=None,
                       order_id=None, validate=None, on_response=None, retries=1):
        """Same contract as chat_json: validated dict, or None when invalid."""
        user = messages[-1]["content"]
        message = user.split("CUSTOMER MESSAGE:", 1)[1].split("Return the JSON object only.")[0].strip()
        self.calls.append({"model": model, "purpose": purpose, "system": messages[0]["content"], "user": user,
                           "message": message, "client_id": client_id, "conversation_id": conversation_id})
        entry = self.script.get(message.lower())
        assert entry is not None, f"router called for unscripted message {message!r}"
        if isinstance(entry, Exception):
            raise entry
        return entry if validate is None or validate(entry) else None


@pytest.fixture
def router(monkeypatch):
    """Install the scripted router as llm_client.chat_json (what intent_router.call_router uses)."""
    fake = ScriptedRouter()
    monkeypatch.setattr("app.services.llm_client.chat_json", fake)
    return fake


# ── seeding ──────────────────────────────────────────────────────────────────

async def seed_shop(session: AsyncSession, phone: str, pnid: str, *, router_on: bool | None = True):
    """
    Client (COD + UPI, 3–7 business days) with:
      LH10042 Cotton Lehenga ₹500 — Pink/Blue × M/XXL (Pink XXL sold out)
      SR20001 Green Cotton Saree ₹899, SR20002 Red Silk Saree ₹2,499, SR20003 Blue Cotton Saree ₹950
      KU30001 Printed Kurti ₹450 (simple, stock 10)
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Router Test Store", email=f"router_{phone}@test.com", phone=phone[:-1] + "0",
        whatsapp_phone_number_id=pnid, accepts_cod=True, accepts_upi=True, upi_id="shop@upi",
        hashed_password="x", delivery_days_min=3, delivery_days_max=7, router_v2_enabled=router_on,
        catalogue_slug=f"router-{phone[-4:]}",
    )
    session.add(client)
    await session.flush()

    def _prod(sku, name, price, category, stock=0, has_variants=False):
        """Add a product row."""
        p = Product(client_id=client.id, name=name, sku=sku, price=price, stock=stock, is_active=True,
                    has_variants=has_variants, category=category)
        session.add(p)
        return p

    lehenga = _prod("LH10042", "Cotton Lehenga", 500.0, "Lehenga", 13, True)
    sarees = [
        _prod("SR20001", "Green Cotton Saree", 899.0, "Saree", 4, True),
        _prod("SR20002", "Red Silk Saree", 2499.0, "Saree", 2, True),
        _prod("SR20003", "Blue Cotton Saree", 950.0, "Saree", 3, True),
    ]
    kurti = _prod("KU30001", "Printed Kurti", 450.0, "Kurti", 10)
    await session.flush()
    for color, size, stock in (("Pink", "M", 5), ("Pink", "XXL", 0), ("Blue", "M", 5), ("Blue", "XXL", 3)):
        session.add(ProductVariant(product_id=lehenga.id, client_id=client.id, color=color, size=size,
                                   stock=stock, is_active=True, price=500.0))
    for sp, color, stock in zip(sarees, ("Green", "Red", "Blue"), (4, 2, 3)):
        session.add(ProductVariant(product_id=sp.id, client_id=client.id, color=color, size=None,
                                   stock=stock, is_active=True, price=sp.price))
    await session.commit()
    await session.refresh(client)
    await session.refresh(lehenga)
    return client, lehenga, kurti


async def seed_order(session: AsyncSession, client, phone: str, *, status="paid", number="ORD-2026-0007",
                     payment_method="UPI", created_days_ago=2, paid_days_ago: int | None = 1, **extra):
    """Insert an order for `phone` (flat columns only — no line items)."""
    from app.models.order import Order

    now = datetime.now(timezone.utc)
    order = Order(
        order_number=number, client_id=client.id, customer_name="Test Customer", customer_phone=phone,
        delivery_address="1 Test Road", product_name="Cotton Lehenga", product_sku="LH10042", quantity=2,
        unit_price=500.0, total_amount=1000.0, payment_method=payment_method, status=status,
        created_at=now - timedelta(days=created_days_ago),
        paid_at=(now - timedelta(days=paid_days_ago)) if paid_days_ago is not None else None, **extra,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def prime_conv(session: AsyncSession, *, phone: str, client_id: int, stage: str, **fields) -> int:
    """Insert a conversation with a pre-set stage / pinned product / slots."""
    from app.models.conversation import Conversation

    fields.setdefault("flow_state_at", datetime.now(timezone.utc) - timedelta(minutes=2))
    conv = Conversation(phone_number=phone, channel="whatsapp", client_id=client_id, current_stage=stage, **fields)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def get_conv(session: AsyncSession, conv_id: int):
    """Fresh Conversation row."""
    from app.models.conversation import Conversation

    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id).execution_options(populate_existing=True))
    return r.scalar_one()


async def messages(session: AsyncSession, conv_id: int) -> list:
    """All stored messages of a conversation, oldest first."""
    from app.models.message import Message

    r = await session.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
        .execution_options(populate_existing=True))
    return list(r.scalars().all())


async def say(http, captured: list[str], phone: str, pnid: str, text: str) -> str:
    """Send one customer message; return everything the bot sent in reply (joined)."""
    before = len(captured)
    resp = await send_message(http, phone, text, phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    return "\n".join(captured[before:])


def ph(n: str) -> tuple[str, str]:
    """(phone, phone_number_id) unique per test."""
    return f"91950000{n}", f"777{n}"


def eta_dates(base: datetime, lo=3, hi=7) -> tuple[str, str]:
    """Independent re-derivation of the expected ETA window (IST date + business days) as '%d %b'."""
    from zoneinfo import ZoneInfo

    start = base.astimezone(ZoneInfo("Asia/Kolkata")).date()

    def add(d: date, n: int) -> date:
        """Add n Mon–Fri business days."""
        while n > 0:
            d += timedelta(days=1)
            n -= d.weekday() < 5
        return d

    return f"{add(start, lo):%d %b}", f"{add(start, hi):%d %b}"


# ── 1. order status / ETA ────────────────────────────────────────────────────

async def test_status_then_when_will_i_get_it_is_delivery_focus_with_engine_dates(replay_http, replay_session, router, monkeypatch):
    """'where is my order' → status; then 'when I will get?' → order_status focus=delivery with DB-computed dates."""
    phone, pnid = ph("0001")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    order = await seed_order(replay_session, client, phone, status="paid", paid_days_ago=1)
    router.when("where is my order", "order_status", {"focus": "status"}, reply_hint=f"{POISON} shipped yesterday")
    router.when("when I will get?", "order_status", {"focus": "delivery"}, reply_hint=f"{POISON} 12 Dec")

    first = await say(replay_http, captured, phone, pnid, "where is my order")
    second = await say(replay_http, captured, phone, pnid, "when I will get?")

    assert "ORD-2026-0007" in first and "Cotton Lehenga" in first and "₹1,000" in first and "Confirmed" in first
    d_from, d_to = eta_dates(order.paid_at)
    assert f"Expected delivery: {d_from} – {d_to}." in second
    assert POISON not in first + second and AI_SENTINEL not in first + second
    assert [c["purpose"] for c in router.calls] == ["router", "router"]
    assert router.calls[0]["model"] == "openai/gpt-oss-20b" and "JSON" in router.calls[0]["system"]
    conv_id = (await messages(replay_session, 1))[0].conversation_id
    conv = await get_conv(replay_session, conv_id)
    assert conv.ai_enabled is True and conv.pending_product_sku is None       # a status question changes no state
    stored = await messages(replay_session, conv_id)
    assert [m.content for m in stored if m.role == "user"] == ["where is my order", "when I will get?"]


async def test_hinglish_kab_aayega_mera_parcel_is_order_status_in_hindi_templates(replay_http, replay_session, router, monkeypatch):
    """'kab aayega mera parcel' → order_status(delivery), answered from the Hindi-roman templates."""
    phone, pnid = ph("0002")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    order = await seed_order(replay_session, client, phone, status="processing", paid_days_ago=1)
    router.when("kab aayega mera parcel", "order_status", {"focus": "delivery"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "kab aayega mera parcel")

    d_from, d_to = eta_dates(order.paid_at)
    assert f"Delivery ka anumaan: {d_from} – {d_to}." in reply and "Order #ORD-2026-0007" in reply
    assert AI_SENTINEL not in reply


async def test_parcel_kidhar_hai_bhai_dispatched_shows_courier_and_tracking(replay_http, replay_session, router, monkeypatch):
    """'parcel kidhar hai bhai' → order_status(status): dispatched order with courier + tracking from the DB."""
    phone, pnid = ph("0003")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, phone, status="dispatched", courier_name="Delhivery",
                     tracking_number="DLV123456", dispatched_at=datetime.now(timezone.utc))
    router.when("parcel kidhar hai bhai", "order_status", {"focus": "status"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "parcel kidhar hai bhai")

    assert "Dispatch ho gaya" in reply and "Courier: Delhivery" in reply and "DLV123456" in reply
    assert "Delivery ka anumaan" in reply


async def test_order_status_gujarati_script_and_no_orders_and_other_customers_orders_hidden(replay_http, replay_session, router, monkeypatch):
    """Gujarati-script question → Gujarati-script reply; a customer with no orders gets 'no orders' (never someone else's)."""
    phone, pnid = ph("0004")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, "919111111111", status="paid")          # someone else's order
    router.when("મારો ઓર્ડર ક્યાં છે", "order_status", {"focus": "status"}, language="gu")

    reply = await say(replay_http, captured, phone, pnid, "મારો ઓર્ડર ક્યાં છે")

    assert "ORD-2026-0007" not in reply and "router-0004" in reply        # no-orders template + catalogue link
    assert re.search(r"[઀-૿]", reply)                          # Gujarati script


async def test_order_status_pending_upi_payment_shows_reminder_not_eta(replay_http, replay_session, router, monkeypatch):
    """Unpaid UPI order, 'when will it come' → the delivery clock hasn't started; payment instructions are shown."""
    phone, pnid = ph("0005")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, phone, status="pending_payment", paid_days_ago=None)
    router.when("kab milega", "order_status", {"focus": "delivery"}, language="hinglish")
    router.when("payment hua kya", "order_status", {"focus": "payment"}, language="hinglish")

    delivery = await say(replay_http, captured, phone, pnid, "kab milega")
    payment = await say(replay_http, captured, phone, pnid, "payment hua kya")

    assert "payment verify hone ke baad delivery (3–7 working days) shuru hogi" in delivery
    assert "shop@upi" in payment and "₹1,000" in payment


# ── 2. catalogue search / product questions ──────────────────────────────────

async def test_green_saree_under_1000_filters_in_db_and_shows_the_single_match(replay_http, replay_session, router, monkeypatch):
    """search_catalog(color=green, max_price=1000) → only SR20001; one hit = the normal product card + pin."""
    phone, pnid = ph("0006")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("green saree under 1000", "search_catalog",
                {"query": "saree", "filters": {"color": "green", "max_price": 1000}}, reply_hint=f"{POISON} ₹1")

    reply = await say(replay_http, captured, phone, pnid, "green saree under 1000")

    assert "Green Cotton Saree [SR20001] — ₹899" in reply
    assert "SR20002" not in reply and "2,499" not in reply and POISON not in reply and AI_SENTINEL not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "SR20001"
    # the router saw the saree candidates (fuzzy search) in its prompt
    assert "sku=SR20001" in router.calls[0]["user"]


async def test_search_with_several_hits_lists_them_opens_a_pick_menu_and_number_reply_skips_the_router(replay_http, replay_session, router, monkeypatch):
    """'sarees under 1000' → numbered list of the 2 matches + pending_choice_skus; '2' is a ₹0 menu pick."""
    phone, pnid = ph("0007")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("sarees under 1000", "search_catalog", {"query": "saree", "filters": {"max_price": 1000}})

    listing = await say(replay_http, captured, phone, pnid, "sarees under 1000")

    assert "1. Green Cotton Saree [SR20001] — ₹899" in listing and "2. Blue Cotton Saree [SR20003] — ₹950" in listing
    assert "Silk" not in listing
    conv_id = (await messages(replay_session, 1))[0].conversation_id
    assert json.loads((await get_conv(replay_session, conv_id)).pending_choice_skus) == ["SR20001", "SR20003"]

    picked = await say(replay_http, captured, phone, pnid, "2")

    assert len(router.calls) == 1                         # the pick never reached the router
    assert "Blue Cotton Saree" in picked and "950" in picked
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "SR20003"


async def test_search_with_no_match_is_generic_never_echoes_text_and_offers_closest_in_stock(replay_http, replay_session, router, monkeypatch):
    """'purple saree under 300' → generic not-found + closest sarees; the customer's words are never repeated."""
    phone, pnid = ph("0008")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("purple saree under 300", "search_catalog",
                {"query": "saree", "filters": {"color": "purple", "max_price": 300}})

    reply = await say(replay_http, captured, phone, pnid, "purple saree under 300")

    assert "we don't have an exact match" in reply and "closest options" in reply
    assert "purple" not in reply.lower() and "300" not in reply
    assert "[SR200" in reply and "Kurti" not in reply and "Lehenga" not in reply     # same category only
    conv_id = (await messages(replay_session, 1))[0].conversation_id
    assert json.loads((await get_conv(replay_session, conv_id)).pending_choice_skus)  # pick menu open for the alternatives


async def test_xxl_hai_with_pinned_lehenga_answers_sizes_from_db(replay_http, replay_session, router, monkeypatch):
    """'XXL hai?' with a pinned lehenga → yes (Blue XXL in stock); 'XXL pink hai?' → no + the sizes Pink really has."""
    phone, pnid = ph("0009")
    captured = capture_all(monkeypatch)
    client, lehenga, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                               pending_product_sku="LH10042", last_shown_sku="LH10042")
    router.when("XXL hai?", "show_product", {"sku": "LH10042", "filters": {"size": "XXL"}}, language="hinglish")
    router.when("XXL pink hai?", "show_product", {"sku": "LH10042", "filters": {"size": "XXL", "color": "Pink"}},
                language="hinglish")

    yes = await say(replay_http, captured, phone, pnid, "XXL hai?")
    no = await say(replay_http, captured, phone, pnid, "XXL pink hai?")

    assert "Haan, Cotton Lehenga mein XXL available hai" in yes
    assert "Cotton Lehenga mein XXL / Pink available nahi hai" in no or "Pink / XXL" in no or "XXL" in no
    assert "Available sizes: M" in no
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "LH10042" and conv.current_stage == "product_inquiry"
    assert AI_SENTINEL not in yes + no


async def test_show_product_without_filters_uses_the_deterministic_card_and_pins(replay_http, replay_session, router, monkeypatch):
    """show_product(sku) → exact-SKU path: compact card from the DB, product pinned, zero LLM reply calls."""
    phone, pnid = ph("0010")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("kurti ka rate batao", "show_product", {"sku": "KU30001"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "kurti ka rate batao")

    assert "Printed Kurti [KU30001] — ₹450" in reply and "Would you like to order?" in reply
    assert AI_SENTINEL not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "KU30001"
    stored = await messages(replay_session, conv.id)
    assert stored[0].content == "kurti ka rate batao"        # the customer's own words, not the canonical SKU


async def test_product_with_a_non_standard_sku_is_pinned_via_its_unique_name(replay_http, replay_session, router, monkeypatch):
    """SKU 'VR_0001' isn't AB12345-shaped, so legacy can't match it as text: the router hands over the exact name instead."""
    from app.models.product import Product

    phone, pnid = ph("0039")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    replay_session.add(Product(client_id=client.id, name="Silk Stole", sku="VR_0001", price=300.0, stock=3,
                               is_active=True, category="Stole"))
    await replay_session.commit()
    router.when("silk stole ka rate", "show_product", {"sku": "VR_0001"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "silk stole ka rate")

    assert "Silk Stole [VR_0001] — ₹300" in reply and AI_SENTINEL not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "VR_0001"


async def test_hallucinated_sku_is_dropped_never_executed(replay_http, replay_session, router, monkeypatch):
    """A sku the model invents (not a candidate) is dropped → search by query instead; nothing is pinned to it."""
    phone, pnid = ph("0011")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("show me the kurti", "show_product", {"sku": "XX99999", "query": "kurti"})

    reply = await say(replay_http, captured, phone, pnid, "show me the kurti")

    assert "XX99999" not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku in (None, "KU30001")


# ── 2b. photo requests / repeated replies ────────────────────────────────────

def capture_images(monkeypatch) -> list[tuple[str, str | None]]:
    """Record every (url, caption) the WhatsApp adapter sends as an image (the gate/Meta call is stubbed)."""
    sent: list[tuple[str, str | None]] = []

    async def _fake_send_image(to, url, caption=None, **_kw):
        """Pretend the image was delivered."""
        sent.append((url, caption))
        return {"messages": [{"id": "wamid.img"}]}

    async def _no_watermark(client_id, sku, url):
        """Skip R2/watermarking: send the stored URL as is."""
        return url

    monkeypatch.setattr("app.routers._whatsapp_adapter.outbound.send_image", _fake_send_image)
    monkeypatch.setattr("app.services.storage_service.get_watermarked_image_url", _no_watermark)
    return sent


async def _set_image(session, sku: str, url: str) -> None:
    """Give a seeded product a photo."""
    from app.models.product import Product

    product = (await session.execute(select(Product).where(Product.sku == sku))).scalar_one()
    product.image_url = url
    await session.commit()


async def test_images_with_a_pinned_product_sends_its_photo_not_the_same_card_again(replay_http, replay_session, router, monkeypatch):
    """'Imagea' (typo) with a pinned product that HAS a photo → the photo goes out; the router flag isn't even needed."""
    phone, pnid = ph("0040")
    captured = capture_all(monkeypatch)
    images = capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await _set_image(replay_session, "KU30001", "https://img.example.com/ku30001.jpg")
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                     pending_product_sku="KU30001", last_shown_sku="KU30001")
    router.when("imagea", "show_product", {"sku": "KU30001"})       # model forgot want_photo: engine still sees the typo

    reply = await say(replay_http, captured, phone, pnid, "Imagea")

    assert images == [("https://img.example.com/ku30001.jpg", None)]
    assert "Printed Kurti [KU30001] — ₹450" in reply


async def test_photo_request_for_a_product_without_a_photo_says_so_instead_of_repeating_the_card(replay_http, replay_session, router, monkeypatch):
    """No image on file → an honest 'no photo yet' line; the reply differs from the card shown before."""
    phone, pnid = ph("0041")
    captured = capture_all(monkeypatch)
    images = capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                               pending_product_sku="KU30001", last_shown_sku="KU30001")
    router.when("images", "show_product", {"sku": "KU30001", "want_photo": True})

    reply = await say(replay_http, captured, phone, pnid, "Images")

    assert images == []
    assert "don't have a photo of Printed Kurti" in reply and "Printed Kurti [KU30001] — ₹450" in reply
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "KU30001"


async def test_photo_request_misread_as_smalltalk_with_a_pinned_product_is_promoted_to_show_product(replay_http, replay_session, router, monkeypatch):
    """The model calls 'photos please' smalltalk → the engine promotes it (pinned product + photo wording)."""
    phone, pnid = ph("0042")
    captured = capture_all(monkeypatch)
    images = capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await _set_image(replay_session, "KU30001", "https://img.example.com/ku30001.jpg")
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                     pending_product_sku="KU30001", last_shown_sku="KU30001")
    router.when("photos please", "smalltalk", confidence=0.6, reply_hint="Sure!")

    await say(replay_http, captured, phone, pnid, "photos please")

    assert len(images) == 1


async def test_give_me_images_after_a_search_list_sends_the_photos_of_the_listed_products(replay_http, replay_session, router, monkeypatch):
    """A search list + 'give me images' → the same list WITH the products' photos (captioned), not a bare repeat."""
    phone, pnid = ph("0043")
    captured = capture_all(monkeypatch)
    images = capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await _set_image(replay_session, "SR20001", "https://img.example.com/sr20001.jpg")
    await _set_image(replay_session, "SR20003", "https://img.example.com/sr20003.jpg")
    router.when("sarees under 1000", "search_catalog", {"query": "saree", "filters": {"max_price": 1000}})
    router.when("give me images", "search_catalog", {"query": "saree", "filters": {"max_price": 1000}, "want_photo": True})

    first = await say(replay_http, captured, phone, pnid, "sarees under 1000")
    assert images == []
    second = await say(replay_http, captured, phone, pnid, "Give me images")

    assert [u for u, _ in images] == ["https://img.example.com/sr20001.jpg", "https://img.example.com/sr20003.jpg"]
    assert images[0][1] == "Green Cotton Saree [SR20001] — ₹899"
    assert "Green Cotton Saree [SR20001]" in first and "Green Cotton Saree [SR20001]" in second


async def test_resending_the_pinned_sku_mid_order_keeps_the_slots_and_shows_the_card(replay_http, replay_session, router, monkeypatch):
    """Customer is at 'Quantity?' for KU30001 and sends 'KU30001' again → card + the same question, nothing reset."""
    phone, pnid = ph("0044")
    captured = capture_all(monkeypatch)
    images = capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await _set_image(replay_session, "KU30001", "https://img.example.com/ku30001.jpg")
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="KU30001", last_shown_sku="KU30001",
                               customer_name="Asha", delivery_address="1 Test Road")

    reply = await say(replay_http, captured, phone, pnid, "KU30001")

    assert "Printed Kurti [KU30001] — ₹450" in reply and "Quantity" in reply
    assert [u for u, _ in images] == ["https://img.example.com/ku30001.jpg"]
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "KU30001" and conv.customer_name == "Asha" and conv.delivery_address == "1 Test Road"


async def test_switching_product_mid_order_shows_the_new_products_card_not_just_the_next_question(replay_http, replay_session, router, monkeypatch):
    """At 'Quantity?' for the kurti the customer sends a saree SKU → switched, with the saree's card before the question."""
    phone, pnid = ph("0045")
    captured = capture_all(monkeypatch)
    capture_images(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="KU30001", last_shown_sku="KU30001")
    router.when("sr20002", "show_product", {"sku": "SR20002"})

    reply = await say(replay_http, captured, phone, pnid, "SR20002")

    assert "Okay, switching to this product:" in reply and "Red Silk Saree [SR20002] — ₹2,499" in reply
    assert (await get_conv(replay_session, conv_id)).pending_product_sku == "SR20002"


def test_wants_photo_matches_typos_but_not_ordinary_words():
    """Typo-tolerant photo detection ('imagea', 'photoo') without catching 'pick', '100 pic' or 'image' lookalikes."""
    from app.schemas.router import RouterDecision
    from app.services.router_actions import wants_photo

    for text in ("Images", "Imagea", "give me photoo", "send picture", "photos"):
        assert wants_photo(None, text), text
    for text in ("pick one", "100 pic leva che", "imagine that", "2", "Green", "KU30001"):
        assert not wants_photo(None, text), text
    flagged = RouterDecision.model_validate({"action": "show_product", "args": {"want_photo": "true"}, "confidence": 0.9})
    assert wants_photo(flagged, "dikhao")


# ── 3. orders: start / answer / change / cancel ──────────────────────────────

async def test_start_order_pins_product_and_asks_first_slot(replay_http, replay_session, router, monkeypatch):
    """'mala lehenga joiye chhe' → start_order(LH10042): pinned, order_collection, colour question — no second 'order?' prompt."""
    phone, pnid = ph("0012")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("book karo lehenga", "start_order", {"sku": "LH10042"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "book karo lehenga")

    assert re.search(r"Colou?r\?", reply) and "Pink" in reply and "Blue" in reply and "Would you like to order?" not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.current_stage == "order_collection" and conv.pending_product_sku == "LH10042"
    assert AI_SENTINEL not in reply


async def test_answer_slot_is_canonicalised_and_applied_by_the_slot_machine(replay_http, replay_session, router, monkeypatch):
    """'pink wala' at the colour question → canonical 'Pink' → slot machine writes it; inbound text stays as typed."""
    phone, pnid = ph("0013")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042")
    router.when("pink wala", "answer_slot", {"slot": "color", "value": "Pink"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "pink wala")

    conv = await get_conv(replay_session, conv_id)
    assert conv.selected_color == "Pink"
    assert "Size" in reply or "size" in reply
    stored = [m.content for m in await messages(replay_session, conv_id) if m.role == "user"]
    assert stored == ["pink wala"]


async def test_answer_slot_with_value_not_offered_reasks_with_options_and_writes_nothing(replay_http, replay_session, router, monkeypatch):
    """The model says colour=Green but only Pink/Blue exist → 'not an available option' + the colour question again."""
    phone, pnid = ph("0014")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042")
    router.when("green please", "answer_slot", {"slot": "color", "value": "Green"})

    reply = await say(replay_http, captured, phone, pnid, "green please")

    assert "isn't one of the available options" in reply and "Pink" in reply and "Blue" in reply
    conv = await get_conv(replay_session, conv_id)
    assert conv.selected_color is None and conv.current_stage == "order_collection"


async def test_name_slot_answer_passes_through_untouched_and_talk_to_someone_there_is_a_handoff(replay_http, replay_session, router, monkeypatch):
    """At the name question: a real name is the answer (text untouched); 'talk to someone' is NOT taken as the name."""
    phone, pnid = ph("0015")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042", selected_color="Blue",
                               selected_size="M", pending_order_quantity=1)
    router.when("Amit Marthak", "answer_slot", {"slot": "name", "value": "Amit Marthak"})
    router.when("I want to talk to someone", "handoff_human")

    await say(replay_http, captured, phone, pnid, "Amit Marthak")
    conv = await get_conv(replay_session, conv_id)
    assert conv.customer_name == "Amit Marthak"

    reply = await say(replay_http, captured, phone, pnid, "I want to talk to someone")
    assert "Our team will reply here shortly." in reply
    assert (await get_conv(replay_session, conv_id)).customer_name == "Amit Marthak"


async def test_make_it_2_instead_of_3_changes_quantity_and_reasks_next_step(replay_http, replay_session, router, monkeypatch):
    """Mid-order change_slot(quantity=2): DB quantity becomes 2, 'Updated' line, then the next open question."""
    phone, pnid = ph("0016")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="LH10042",
        last_shown_sku="LH10042", selected_color="Blue", selected_size="M", pending_order_quantity=3,
        cart_variant_mode="same")
    router.when("make it 2 instead of 3", "change_slot", {"slot": "quantity", "value": "2"})

    reply = await say(replay_http, captured, phone, pnid, "make it 2 instead of 3")

    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_order_quantity == 2
    assert "Updated ✅ Quantity: 2" in reply and "name" in reply.lower()
    assert conv.current_stage == "order_collection" and conv.selected_color == "Blue"


async def test_change_quantity_at_summary_rerenders_summary_with_new_total(replay_http, replay_session, router, monkeypatch):
    """At the confirm step, quantity 3 → 2: summary is re-rendered from the DB (total ₹1,000) with Confirm/Cancel buttons."""
    phone, pnid = ph("0017")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="awaiting_final_confirmation",
        pending_product_sku="LH10042", last_shown_sku="LH10042", selected_color="Blue", selected_size="M",
        pending_order_quantity=3, customer_name="Amit", delivery_address="12 MG Road Surat 395007",
        mobile_number=phone, payment_method="UPI", summary_shown=True, cart_variant_mode="same")
    router.when("2 kar do", "change_slot", {"slot": "quantity", "value": "2"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "2 kar do")

    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_order_quantity == 2 and conv.current_stage == "awaiting_final_confirmation"
    assert conv.summary_shown is True
    assert "1,000" in reply and "1,500" not in reply and "Blue" in reply


async def test_address_badalna_hai_clears_the_address_and_asks_for_it_again(replay_http, replay_session, router, monkeypatch):
    """change_slot(address) with no new value → address cleared, stage back to order_collection, address question asked."""
    phone, pnid = ph("0040")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="awaiting_final_confirmation",
        pending_product_sku="LH10042", last_shown_sku="LH10042", selected_color="Blue", selected_size="M",
        pending_order_quantity=1, customer_name="Amit", delivery_address="12 MG Road Surat 395007",
        mobile_number=phone, payment_method="UPI", summary_shown=True)
    router.when("address badalna hai", "change_slot", {"slot": "address"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "address badalna hai")

    conv = await get_conv(replay_session, conv_id)
    assert conv.delivery_address is None and conv.current_stage == "order_collection" and conv.summary_shown is False
    assert conv.customer_name == "Amit" and "address" in reply.lower()
    assert "12 MG Road" not in reply


async def test_change_quantity_above_stock_is_refused_and_db_unchanged(replay_http, replay_session, router, monkeypatch):
    """Blue/M has 5 in stock → quantity 9 is refused with the stock template; the DB keeps 3."""
    phone, pnid = ph("0018")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="LH10042",
        last_shown_sku="LH10042", selected_color="Blue", selected_size="M", pending_order_quantity=3,
        cart_variant_mode="same")
    router.when("make it 9", "change_slot", {"slot": "quantity", "value": "9"})

    reply = await say(replay_http, captured, phone, pnid, "make it 9")

    assert (await get_conv(replay_session, conv_id)).pending_order_quantity == 3
    assert "5" in reply and "Updated" not in reply


async def test_change_size_to_sold_out_combo_is_refused_with_real_alternatives(replay_http, replay_session, router, monkeypatch):
    """Pink is chosen; changing size to XXL (Pink XXL sold out) is refused and the DB keeps size M."""
    phone, pnid = ph("0019")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="LH10042",
        last_shown_sku="LH10042", selected_color="Pink", selected_size="M", pending_order_quantity=1,
        cart_variant_mode="same")
    router.when("size xxl karo", "change_slot", {"slot": "size", "value": "XXL"}, language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "size xxl karo")

    assert (await get_conv(replay_session, conv_id)).selected_size == "M"
    assert "isn't available in Pink / XXL" in reply or "Pink / XXL" in reply


async def test_change_slot_after_order_placed_is_explained_not_applied(replay_http, replay_session, router, monkeypatch):
    """Stage 'payment' (order already created) → can't edit here; offers a human; DB untouched."""
    phone, pnid = ph("0020")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="payment",
                               pending_product_sku="LH10042", pending_order_quantity=3)
    router.when("make it 2", "change_slot", {"slot": "quantity", "value": "2"})

    reply = await say(replay_http, captured, phone, pnid, "make it 2")

    assert "already been placed" in reply and "talk to someone" in reply
    assert (await get_conv(replay_session, conv_id)).pending_order_quantity == 3


async def test_cancel_order_cancels_unpaid_order_and_resets_the_flow(replay_http, replay_session, router, monkeypatch):
    """cancel_order with an unpaid order: status → cancelled in the DB, slots reset, stage greeting."""
    from app.models.order import Order

    phone, pnid = ph("0021")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="payment",
                               pending_product_sku="LH10042", pending_order_quantity=2)
    order = await seed_order(replay_session, client, phone, status="pending_payment", paid_days_ago=None,
                             conversation_id=conv_id)
    router.when("order cancel kar do", "cancel_order", language="hinglish")

    reply = await say(replay_http, captured, phone, pnid, "order cancel kar do")

    row = (await replay_session.execute(select(Order).where(Order.id == order.id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "cancelled" and row.cancel_reason == "cancelled by customer"
    assert "Order cancel kar diya gaya" in reply
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting" and conv.pending_product_sku is None


async def test_bare_cancel_in_payment_stage_stays_with_the_legacy_guard_and_skips_the_router(replay_http, replay_session, router, monkeypatch):
    """Only the bare 'cancel' (the Cancel button's text) is a ₹0 guard; the router is not called for it."""
    from app.models.order import Order

    phone, pnid = ph("0036")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="payment",
                               pending_product_sku="LH10042", pending_order_quantity=2)
    order = await seed_order(replay_session, client, phone, status="pending_payment", paid_days_ago=None,
                             conversation_id=conv_id)

    reply = await say(replay_http, captured, phone, pnid, "cancel")

    assert router.calls == [] and "Order cancelled" in reply
    row = (await replay_session.execute(select(Order).where(Order.id == order.id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "cancelled"


async def test_cancel_draft_order_mid_collection_resets_slots(replay_http, replay_session, router, monkeypatch):
    """'I don't want this anymore' while collecting slots → cancel_order: draft cleared, back to greeting (no Order row)."""
    from app.models.order import Order

    phone, pnid = ph("0037")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042", selected_color="Blue",
                               selected_size="M", pending_order_quantity=2)
    router.when("i don't want this anymore", "cancel_order")

    reply = await say(replay_http, captured, phone, pnid, "I don't want this anymore")

    assert "Order cancelled" in reply
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting" and conv.pending_product_sku is None
    assert conv.selected_color is None and conv.pending_order_quantity is None
    assert (await replay_session.execute(select(Order))).first() is None


async def test_cancel_order_is_refused_while_payment_is_under_verification_or_already_paid(replay_http, replay_session, router, monkeypatch):
    """payment_submitted → 'with our team for verification'; paid → explained with the status; DB statuses unchanged."""
    from app.models.order import Order

    phone, pnid = ph("0022")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="completed")
    verifying = await seed_order(replay_session, client, phone, status="payment_submitted", number="ORD-2026-0001",
                                 paid_days_ago=None, conversation_id=conv_id)
    router.when("cancel my order", "cancel_order")

    reply = await say(replay_http, captured, phone, pnid, "cancel my order")
    assert "already with our team for verification" in reply
    row = (await replay_session.execute(select(Order).where(Order.id == verifying.id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "payment_submitted"

    row.status = "dispatched"
    await replay_session.commit()
    reply2 = await say(replay_http, captured, phone, pnid, "cancel my order")
    assert "already 'Dispatched" in reply2 and "can't be cancelled here" in reply2


# ── 4. human handoff / greeting / smalltalk / faq ────────────────────────────

async def test_i_want_to_talk_to_someone_pauses_the_bot_and_flags_the_dashboard(replay_http, replay_session, router, monkeypatch):
    """handoff_human: fixed reply, bot paused (ai_enabled False), dashboard event published, later messages stay silent."""
    from app.services import realtime_service

    phone, pnid = ph("0023")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    queue = realtime_service.subscribe(client.id)
    router.when("I want to talk to someone", "handoff_human", reply_hint=f"{POISON}")

    reply = await say(replay_http, captured, phone, pnid, "I want to talk to someone")

    assert reply == "Our team will reply here shortly." and POISON not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.ai_enabled is False and conv.bot_pause_source == "escalation"
    assert conv.taken_over_note and conv.escalation_count == 1
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(e["type"] == "conversation_updated" and e["data"]["bot_paused"] is True for e in events)
    realtime_service.unsubscribe(client.id, queue)

    follow = await say(replay_http, captured, phone, pnid, "hello??")      # paused: saved silently, router not called
    assert follow == "" and len(router.calls) == 1


async def test_hi_without_a_product_gets_the_welcome_and_hi_mid_browse_resumes(replay_http, replay_session, router, monkeypatch):
    """greeting: fresh welcome when nothing is pinned; welcome-back + the pending question when a product is pinned."""
    phone, pnid = ph("0024")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("Hi", "greeting")
    router.when("namaste ji", "greeting", language="hinglish")

    welcome = await say(replay_http, captured, phone, pnid, "Hi")
    assert "Welcome to Router Test Store" in welcome and "router-0024" in welcome

    conv_id = (await messages(replay_session, 1))[0].conversation_id
    conv = await get_conv(replay_session, conv_id)
    conv.pending_product_sku = "LH10042"
    conv.current_stage = "product_inquiry"
    conv.flow_state_at = datetime.now(timezone.utc)
    await replay_session.commit()

    resume = await say(replay_http, captured, phone, pnid, "namaste ji")
    assert "Cotton Lehenga" in resume and re.search(r"Colou?r\?", resume) and "Wapas aaye" in resume
    after = await get_conv(replay_session, conv_id)
    assert after.pending_product_sku == "LH10042" and after.current_stage == "product_inquiry"
    assert AI_SENTINEL not in welcome + resume


async def test_hi_mid_order_collection_resumes_the_open_slot(replay_http, replay_session, router, monkeypatch):
    """'Hi' while the colour question is open → greeting + the same question again; state untouched, no attempt burned."""
    phone, pnid = ph("0025")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042")
    router.when("Hi", "greeting")

    reply = await say(replay_http, captured, phone, pnid, "Hi")

    assert re.search(r"Colou?r\?", reply) and "Pink" in reply and "elcome back" in reply
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "order_collection" and conv.selected_color is None
    assert not conv.slot_attempt_count and AI_SENTINEL not in reply
    assert [m.content for m in await messages(replay_session, conv_id) if m.role == "user"] == ["Hi"]


async def test_gibberish_gets_a_clarifying_question_without_echo_or_state_change(replay_http, replay_session, router, monkeypatch):
    """Low-confidence output → 'what are you looking for?' template; the gibberish is never repeated; nothing changes."""
    phone, pnid = ph("0026")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("asdkjh qwezzz", "smalltalk", confidence=0.1, reply_hint=f"{POISON}")

    reply = await say(replay_http, captured, phone, pnid, "asdkjh qwezzz")

    assert "didn't quite get that" in reply and "order status" in reply
    assert "asdkjh" not in reply and "qwezzz" not in reply and POISON not in reply and AI_SENTINEL not in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.current_stage == "greeting" and conv.pending_product_sku is None and conv.pending_choice_skus is None


async def test_low_confidence_with_two_candidates_asks_did_you_mean_a_or_b(replay_http, replay_session, router, monkeypatch):
    """confidence < 0.5 and two catalogue candidates → 'Did you mean A or B?' from the DB rows."""
    phone, pnid = ph("0027")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("cotton", "search_catalog", {"query": "cotton"}, confidence=0.35)

    reply = await say(replay_http, captured, phone, pnid, "cotton")

    assert reply.startswith("Did you mean ") and "Cotton Lehenga [LH10042]" in reply and "Saree [SR200" in reply


async def test_smalltalk_hint_passes_the_guard_but_unsafe_hint_is_replaced(replay_http, replay_session, router, monkeypatch):
    """A harmless hint is sent; a hint stating a price/delivery claim is swapped for the fixed friendly template."""
    phone, pnid = ph("0028")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    router.when("thanks!", "smalltalk", reply_hint="You're welcome! 😊")
    router.when("how are you", "smalltalk", reply_hint="Great! Everything ships free in 2 days for ₹100.")

    ok = await say(replay_http, captured, phone, pnid, "thanks!")
    unsafe = await say(replay_http, captured, phone, pnid, "how are you")

    assert ok == "You're welcome! 😊"
    assert "free" not in unsafe and "₹100" not in unsafe and "Happy to help" in unsafe


async def test_faq_answers_from_knowledge_base_else_offers_a_human(replay_http, replay_session, router, monkeypatch):
    """faq: the seller's KB answer verbatim; with no KB entry → handoff offer ('talk to someone'), never an invented policy."""
    from app.models.knowledge_base import KnowledgeBase

    phone, pnid = ph("0029")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    replay_session.add(KnowledgeBase(client_id=client.id, question="return policy exchange days",
                                     answer="Exchange within 7 days of delivery, unworn with tags.", is_active=True,
                                     is_approved=True))
    await replay_session.commit()
    router.when("what is your return policy", "faq", {"query": "return policy exchange"})
    router.when("do you gift wrap", "faq", {"query": "gift wrap"}, reply_hint=f"{POISON} yes free")

    kb = await say(replay_http, captured, phone, pnid, "what is your return policy")
    none = await say(replay_http, captured, phone, pnid, "do you gift wrap")

    assert kb == "Exchange within 7 days of delivery, unworn with tags."
    assert "talk to someone" in none and POISON not in none


# ── 5. fast paths, flag, fallback ────────────────────────────────────────────

async def test_fast_paths_never_call_the_router(replay_http, replay_session, router, monkeypatch):
    """Exact SKU, exact option at the open slot and a bare quantity are ₹0: zero router calls, normal behaviour."""
    phone, pnid = ph("0030")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="LH10042", last_shown_sku="LH10042")

    reply = await say(replay_http, captured, phone, pnid, "Pink")

    assert router.calls == []
    assert (await get_conv(replay_session, conv_id)).selected_color == "Pink"
    assert "Size" in reply or "size" in reply

    phone2, pnid2 = ph("0031")
    await seed_shop(replay_session, phone2, pnid2)
    card = await say(replay_http, captured, phone2, pnid2, "KU30001")
    assert router.calls == [] and "Printed Kurti" in card


async def test_yes_after_the_product_card_is_a_fast_path_into_order_collection(replay_http, replay_session, router, monkeypatch):
    """Exact SKU → card; the customer's 'yes' → order_collection — neither turn calls the router (₹0)."""
    phone, pnid = ph("0038")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)

    card = await say(replay_http, captured, phone, pnid, "KU30001")
    ask = await say(replay_http, captured, phone, pnid, "yes")

    assert router.calls == [] and "Printed Kurti" in card
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.current_stage == "order_collection" and conv.pending_product_sku == "KU30001"
    assert ask and AI_SENTINEL not in ask


async def test_flag_off_for_the_client_keeps_the_keyword_pipeline(replay_http, replay_session, router, monkeypatch):
    """Client.router_v2_enabled=False → the router is never called; the keyword order-status detector answers."""
    phone, pnid = ph("0032")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid, router_on=False)
    await seed_order(replay_session, client, phone, status="paid")

    reply = await say(replay_http, captured, phone, pnid, "where is my order")

    assert router.calls == [] and "ORD-2026-0007" in reply


async def test_llm_down_falls_back_to_keyword_detectors_then_the_rephrase_template(replay_http, replay_session, monkeypatch, caplog):
    """
    LLM unavailable (breaker open): the real router call is skipped, the keyword detectors still answer
    'where is my order' / 'Hi', and an open question gets the 'could you rephrase?' template.
    """
    from app.services import gemini_service, llm_health
    from app.services.llm_health import LLMUnavailableError

    phone, pnid = ph("0033")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, phone, status="paid")
    llm_health.reset_state()
    llm_health.trip("test: llm down")
    monkeypatch.setattr(llm_health, "should_attempt_llm", lambda: False)
    monkeypatch.setattr(gemini_service, "generate_reply", _raise(LLMUnavailableError("llm down")))
    try:
        with caplog.at_level(logging.INFO):
            status = await say(replay_http, captured, phone, pnid, "where is my order")
            hello = await say(replay_http, captured, phone, pnid, "Hi")
            open_q = await say(replay_http, captured, phone, pnid, "do you have banarasi dupattas")
    finally:
        llm_health.reset_state()

    assert "ORD-2026-0007" in status and "Welcome" in hello
    assert open_q == "Sorry, could you rephrase? Or type a product code, or 'order status'."   # EN; HI/GU variants exist
    assert "fallback=llm_unavailable" in caplog.text and "ROUTER conv=" in caplog.text


def _raise(exc):
    """Async stand-in that raises `exc`."""
    async def _boom(*_a, **_k):
        raise exc
    return _boom


async def test_invalid_router_output_twice_falls_back_to_keywords(replay_http, replay_session, monkeypatch):
    """chat_json returning None (invalid JSON after the retry) → keyword fallback answers; failure is counted."""
    from app.services import llm_health

    phone, pnid = ph("0034")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, phone, status="paid")
    llm_health.reset_state()

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.llm_client.chat_json", _none)
    reply = await say(replay_http, captured, phone, pnid, "where is my order")

    assert "ORD-2026-0007" in reply and llm_health.failures_last_hour() >= 1
    llm_health.reset_state()


async def test_one_router_log_line_per_turn_with_all_fields(replay_http, replay_session, router, monkeypatch, caplog):
    """ROUTER conv=.. action=.. conf=.. args=.. fast_path=.. ms=.. — one line per turn, fast paths included."""
    phone, pnid = ph("0035")
    captured = capture_all(monkeypatch)
    client, _, _ = await seed_shop(replay_session, phone, pnid)
    await seed_order(replay_session, client, phone, status="paid")
    router.when("where is my order", "order_status", {"focus": "status"}, confidence=0.91)

    with caplog.at_level(logging.INFO, logger="app.services.router_actions"):
        await say(replay_http, captured, phone, pnid, "where is my order")
        await say(replay_http, captured, phone, pnid, "KU30001")

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ROUTER ")]
    assert len(lines) == 2
    assert re.search(r"ROUTER conv=\d+ action=order_status conf=0\.91 args=\{\"focus\":\"status\"\} fast_path=- fallback=- ms=\d+", lines[0])
    assert "action=- conf=- args={} fast_path=exact_sku" in lines[1]


# ── optional live test ───────────────────────────────────────────────────────

@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("RUN_LIVE_LLM") == "1" and os.getenv("GROQ_API_KEY")),
    reason="live LLM test: set RUN_LIVE_LLM=1 and GROQ_API_KEY",
)
async def test_live_router_classifies_the_spec_examples():
    """Hits the real classifier model with the production prompt on the spec's example messages."""
    from app.services import intent_router
    from app.services.llm_health import reset_state

    import asyncio

    reset_state()
    cases = [
        ("kab aayega mera parcel", "order_status"),
        ("parcel kidhar hai bhai", "order_status"),
        ("green saree under 1000", "search_catalog"),
        ("I want to talk to someone", "handoff_human"),
        ("Hi", "greeting"),
    ]
    for text, expected in cases:
        ctx = intent_router.RouterContext(
            shop="name: Test Store | customer language: english | delivery: 3–7 business days | payment: COD, UPI",
            state="stage: greeting\npinned_product: none\nfilled_slots: none\nnext_slot: none (no question is open)",
            orders="- ORD-2026-0007 | status: paid | items: 1× Cotton Lehenga | total ₹500 | payment: UPI | placed 04 Oct | paid 05 Oct | ETA: 09 Oct–14 Oct",
            candidates="- Green Cotton Saree | sku=SR20001 | ₹899 | Saree | colors in stock: Green",
            history="(no earlier messages)",
        )
        await asyncio.sleep(12)          # stay under Groq's free-tier 8,000 tokens/min (a call is ~2k tokens)
        call = await intent_router.call_router(ctx, text, client_id=None, conversation_id=None)
        assert call.decision is not None, (text, call.error)
        assert call.decision.action.value == expected, (text, call.decision)
